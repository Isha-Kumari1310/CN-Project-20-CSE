"""
adaptive_lb.py - Ryu (OpenFlow 1.3) controller: Adaptive Load Balancer.

Run:   python3 controller/run_controller.py --mode adaptive
       python3 controller/run_controller.py --mode static     (round-robin baseline)

What it does
------------
1. ARP proxy for the virtual IP (VIP) and the heartbeat IP.
2. Servers send UDP heartbeats to HB_IP:HB_PORT. They match no flow, so the
   switch sends them to the controller as packet-in; we parse the JSON
   (active connections, slots, service time) -> application load.
3. Every 0.5 s we ask the switch for port statistics -> network load per
   server link (OpenFlow counters).
4. A client's TCP SYN to VIP:8000 is a packet-in. We pick a server
   (lb_policy.py) and install two flows with header rewriting:
        client -> VIP     =>  rewrite dst to the server, output server port
        server -> client  =>  rewrite src back to the VIP, output client port
   All later packets of that connection are forwarded by the switch alone.
5. Failure handling: heartbeat timeout OR port-down event marks a server
   dead; the flows that point to it are deleted so new connections go to the
   remaining servers.
6. Everything interesting is written as JSON lines to $LB_LOG for analysis.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lb_policy import LoadBalancer  # noqa: E402

# ---------------------------------------------------------------------------
# Ryu / os-ken compatibility.
# The original Ryu 4.34 does not start on Python >= 3.10 (eventlet API change).
# os-ken is the maintained, API-identical fork of Ryu, so we accept either.
# controller/run_controller.py sets LB_BACKEND to the package that works.
# ---------------------------------------------------------------------------
import importlib  # noqa: E402


def _load_backend():
    forced = os.environ.get("LB_BACKEND")
    order = [forced] if forced else ["os_ken", "ryu"]
    errors = []
    for pkg in order:
        try:
            mod = lambda n: importlib.import_module(pkg + "." + n)  # noqa: E731
            m = {
                "pkg": pkg,
                "app_manager": mod("base.app_manager"),
                "ofp_event": mod("controller.ofp_event"),
                "handler": mod("controller.handler"),
                "hub": mod("lib.hub"),
                "ofproto_v1_3": mod("ofproto.ofproto_v1_3"),
            }
            for n in ("arp", "ethernet", "ether_types", "ipv4", "packet", "tcp", "udp"):
                m[n] = mod("lib.packet." + n)
            return m
        except Exception as exc:          # ImportError or eventlet breakage
            errors.append("%s: %s: %s" % (pkg, type(exc).__name__, exc))
    raise ImportError("no usable OpenFlow controller framework found.\n  "
                      + "\n  ".join(errors)
                      + "\nRun ./setup.sh (it installs os-ken in a virtualenv).")


_B = _load_backend()
app_manager, ofp_event, hub = _B["app_manager"], _B["ofp_event"], _B["hub"]
arp, ethernet, ether_types = _B["arp"], _B["ethernet"], _B["ether_types"]
ipv4, packet, tcp, udp = _B["ipv4"], _B["packet"], _B["tcp"], _B["udp"]
ofproto_v1_3 = _B["ofproto_v1_3"]
CONFIG_DISPATCHER = _B["handler"].CONFIG_DISPATCHER
MAIN_DISPATCHER = _B["handler"].MAIN_DISPATCHER
set_ev_cls = _B["handler"].set_ev_cls

VIP = os.environ.get("LB_VIP", "10.0.0.100")
VMAC = os.environ.get("LB_VMAC", "00:00:00:00:01:00")
HB_IP = os.environ.get("LB_HB_IP", "10.0.0.254")
HB_PORT = int(os.environ.get("LB_HB_PORT", "9999"))
LB_TCP_PORT = int(os.environ.get("LB_TCP_PORT", "8000"))
PRIO_L2, PRIO_LB = 1, 10


BaseApp = getattr(app_manager, "RyuApp", None) or getattr(app_manager, "OSKenApp")


class AdaptiveLB(BaseApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(AdaptiveLB, self).__init__(*args, **kwargs)
        mode = os.environ.get("LB_MODE", "adaptive")
        self.requested_mode = mode
        self._auto_calm_ticks = 0
        self._auto_sjf_ticks = 0
        effective_mode = "static" if mode == "auto" else mode
        link_mbps = float(os.environ.get("LB_LINK_BW_MBPS", "10"))
        self.lb = LoadBalancer(
            mode=effective_mode,
            w_time=float(os.environ.get("LB_W_TIME", "0.7")),
            w_net=float(os.environ.get("LB_W_NET", "0.3")),
            overload_util=float(os.environ.get("LB_OVERLOAD", "1.25")),
            hb_timeout=float(os.environ.get("LB_HB_TIMEOUT", "2.0")),
            link_bps=link_mbps * 1e6)
        self.flow_idle = int(os.environ.get("LB_FLOW_IDLE", "5"))
        self.log_path = os.environ.get("LB_LOG", "/tmp/lb_events.jsonl")
        open(self.log_path, "w").close()
        self.hosts = {}          # ip  -> (mac, port)
        self.mac_to_port = {}    # mac -> port
        self.flows = {}          # (client_ip, client_sport) -> server name
        self.control_path = os.environ.get("LB_CONTROL", "/tmp/lb_control.json")
        self.status_path = os.environ.get("LB_STATUS", "/tmp/lb_effective_mode.json")
        self._overload_state = {}
        self.dp = None
        self._mon = None
        self.logger.info("AdaptiveLB started in %s mode", mode)
        self._ev("start", mode=mode, effective_mode=self.lb.mode)

    # ------------------------------------------------------------------ log
    def _ev(self, kind, **kw):
        rec = {"t": round(time.time(), 4), "ev": kind}
        rec.update(kw)
        with open(self.log_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    # --------------------------------------------------- switch connection
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_features(self, ev):
        dp = ev.msg.datapath
        ofp, p = dp.ofproto, dp.ofproto_parser
        self.dp = dp
        # table-miss: send unmatched packets to the controller
        self._add_flow(dp, 0, p.OFPMatch(),
                       [p.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        if self._mon is None:
            self._mon = hub.spawn(self._monitor)
        self._ev("switch_connected", dpid=dp.id)

    def _add_flow(self, dp, priority, match, actions, idle=0):
        ofp, p = dp.ofproto, dp.ofproto_parser
        inst = [p.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(p.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                 instructions=inst, idle_timeout=idle))

    # --------------------------------------------------------- packet-in
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        self.mac_to_port[eth.src] = in_port

        a = pkt.get_protocol(arp.arp)
        if a is not None:
            return self._handle_arp(dp, msg, in_port, eth, a)

        ip4 = pkt.get_protocol(ipv4.ipv4)
        if ip4 is not None:
            self.hosts[ip4.src] = (eth.src, in_port)
            u = pkt.get_protocol(udp.udp)
            if u is not None and ip4.dst == HB_IP and u.dst_port == HB_PORT:
                return self._handle_heartbeat(msg, in_port, eth, ip4, u)
            t = pkt.get_protocol(tcp.tcp)
            if t is not None and ip4.dst == VIP and t.dst_port == LB_TCP_PORT:
                return self._handle_vip_tcp(dp, msg, in_port, eth, ip4, t)
        self._l2_forward(dp, msg, in_port, eth.dst)

    # ------------------------------------------------------------- ARP
    def _handle_arp(self, dp, msg, in_port, eth, a):
        self.hosts[a.src_ip] = (a.src_mac, in_port)
        if a.opcode == arp.ARP_REQUEST:
            if a.dst_ip in (VIP, HB_IP):
                mac = VMAC
            elif a.dst_ip in self.hosts:
                mac = self.hosts[a.dst_ip][0]
            else:
                mac = None
            if mac is not None:
                return self._send_arp_reply(dp, in_port, mac, a.dst_ip,
                                            a.src_mac, a.src_ip)
        self._l2_forward(dp, msg, in_port, eth.dst)

    def _send_arp_reply(self, dp, port, src_mac, src_ip, dst_mac, dst_ip):
        ofp, p = dp.ofproto, dp.ofproto_parser
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                           dst=dst_mac, src=src_mac))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=src_mac,
                                 src_ip=src_ip, dst_mac=dst_mac, dst_ip=dst_ip))
        pkt.serialize()
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                   in_port=ofp.OFPP_CONTROLLER,
                                   actions=[p.OFPActionOutput(port)],
                                   data=pkt.data))

    # --------------------------------------------- application heartbeat
    def _handle_heartbeat(self, msg, in_port, eth, ip4, u):
        off = 14 + ip4.header_length * 4 + 8
        payload = msg.data[off:off + u.total_length - 8]
        try:
            hb = json.loads(payload.decode())
        except (ValueError, UnicodeDecodeError):
            return
        s, came_up = self.lb.on_heartbeat(hb, ip4.src, eth.src, in_port, time.time())
        if came_up:
            self._ev("server_up", server=s.name, ip=s.ip, port=s.port)
            self.logger.info("server %s UP (%s, port %d)", s.name, s.ip, s.port)

    # ------------------------------------------------- load-balanced TCP
    def _handle_vip_tcp(self, dp, msg, in_port, eth, ip4, t):
        now = time.time()
        key = (ip4.src, t.src_port)
        syn = bool(t.bits & 0x02) and not (t.bits & 0x10)
        s = None
        if syn:
            s = self.lb.pick(now)
            if s is None:
                self._ev("no_server", client=ip4.src, sport=t.src_port)
                return
            self.flows[key] = s.name
            self._ev("assign", client=ip4.src, sport=t.src_port, server=s.name,
                     mode=self.lb.mode, est_active=self.lb.est_active(s),
                     score=round(self.lb.score(s), 3))
        else:
            # flow expired mid-connection: keep the same server (stickiness)
            name = self.flows.get(key)
            s = self.lb.servers.get(name) if name else None
            if s is None:
                return
            if self.lb.mode in ("adaptive", "sjf") and not self.lb.is_alive(s, now):
                return
        self._install_vip_flows(dp, msg, in_port, eth, ip4, t, s)

    def _install_vip_flows(self, dp, msg, in_port, eth, ip4, t, s):
        ofp, p = dp.ofproto, dp.ofproto_parser
        fwd_actions = [p.OFPActionSetField(eth_dst=s.mac),
                       p.OFPActionSetField(ipv4_dst=s.ip),
                       p.OFPActionOutput(s.port)]
        self._add_flow(dp, PRIO_LB, p.OFPMatch(
            eth_type=0x0800, ip_proto=6, ipv4_src=ip4.src, ipv4_dst=VIP,
            tcp_src=t.src_port, tcp_dst=LB_TCP_PORT), fwd_actions, self.flow_idle)
        rev_actions = [p.OFPActionSetField(eth_src=VMAC),
                       p.OFPActionSetField(ipv4_src=VIP),
                       p.OFPActionOutput(in_port)]
        self._add_flow(dp, PRIO_LB, p.OFPMatch(
            eth_type=0x0800, ip_proto=6, ipv4_src=s.ip, ipv4_dst=ip4.src,
            tcp_src=LB_TCP_PORT, tcp_dst=t.src_port), rev_actions, self.flow_idle)
        # send the packet that triggered the packet-in along its way
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                   in_port=in_port, actions=fwd_actions,
                                   data=msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None))

    # ------------------------------------------------- plain L2 forwarding
    def _l2_forward(self, dp, msg, in_port, dst):
        ofp, p = dp.ofproto, dp.ofproto_parser
        out = self.mac_to_port.get(dst, ofp.OFPP_FLOOD)
        actions = [p.OFPActionOutput(out)]
        if out != ofp.OFPP_FLOOD and dst != VMAC:
            self._add_flow(dp, PRIO_L2, p.OFPMatch(eth_dst=dst), actions, 5)
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                   in_port=in_port, actions=actions,
                                   data=msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None))

    # ------------------------------------------------------ network load
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def on_port_stats(self, ev):
        now = time.time()
        for st in ev.msg.body:
            self.lb.update_net(st.port_no, st.rx_bytes, now)

    # ---------------------------------------------------- link failures
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def on_port_status(self, ev):
        msg = ev.msg
        ofp = msg.datapath.ofproto
        d = msg.desc
        down = bool(d.state & ofp.OFPPS_LINK_DOWN) or \
            bool(d.config & ofp.OFPPC_PORT_DOWN) or msg.reason == ofp.OFPPR_DELETE
        s, changed = self.lb.set_port_down(d.port_no, down, time.time())
        if s is None:
            return
        self._ev("port_status", server=s.name, port=d.port_no, down=down)
        if down and changed:
            self._server_down(s, "port_down")

    # ------------------------------------------------------- monitoring
    def _monitor(self):
        tick = 0
        while True:
            hub.sleep(0.5)
            now = time.time()
            # Runtime mode changes are intentionally tiny and file-based so the
            # standard-library UI can control the existing controller safely.
            self._read_control_mode(now)
            for s in self.lb.expire(now):
                self._server_down(s, "heartbeat_timeout")
            self._log_overload_transitions()
            self._write_mode_status(now)
            if self.dp is not None:
                p = self.dp.ofproto_parser
                self.dp.send_msg(p.OFPPortStatsRequest(self.dp, 0,
                                                       self.dp.ofproto.OFPP_ANY))
            tick += 1
            if tick % 2 == 0:
                self._ev("state", servers=self.lb.snapshot(now))

    def _write_mode_status(self, now):
        """Publish the authoritative controller mode; don't infer it from
        a truncated history of mode-change log events."""
        path = self.status_path
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w") as fh:
                json.dump({"t": now, "requested_mode": self.requested_mode,
                           "effective_mode": self.lb.mode}, fh)
            os.replace(tmp_path, path)
        except OSError as exc:
            self.logger.warning("Cannot update effective-mode status: %s", exc)

    def _read_control_mode(self, now=None):
        """Manual static/adaptive, or AUTO: RR when calm and adaptive when stressed.

        AUTO uses two thresholds (hysteresis) to avoid switching repeatedly.
        Each automatic change affects NEW connections; existing OpenFlow
        connections keep their selected backend until their flow expires.
        """
        now = time.time() if now is None else now
        try:
            with open(self.control_path) as fh:
                requested = json.load(fh).get("mode")
            if requested in ("static", "adaptive", "sjf", "auto"):
                self.requested_mode = requested
        except (OSError, ValueError, TypeError):
            pass

        old = self.lb.mode
        if self.requested_mode != "auto":
            target = self.requested_mode
            self._auto_calm_ticks = 0
            self._auto_sjf_ticks = 0
            reason = "manual_selection"
        else:
            servers = list(self.lb.servers.values())
            stressed = bool(servers) and any(
                (not self.lb.is_alive(s, now))
                or self.lb.util(s) >= 0.80
                or self.lb.net_util(s) >= 0.75
                for s in servers
            )
            calm = bool(servers) and all(
                self.lb.is_alive(s, now)
                and self.lb.util(s) <= 0.35
                and self.lb.net_util(s) <= 0.30
                for s in servers
            )
            # SJF is helpful only when the servers have actually observed
            # mixed job sizes. A rolling workload window is reported in
            # heartbeats. We need two checks to avoid one-off flips.
            mixed = bool(servers) and any(
                s.job_mix and s.job_samples >= 8 for s in servers
            )
            moderate = bool(servers) and any(
                0.10 <= self.lb.util(s) < 0.80 for s in servers
            )
            sjf_candidate = mixed and moderate and all(
                self.lb.is_alive(s, now) for s in servers
            )
            if stressed:
                self._auto_calm_ticks = 0
                self._auto_sjf_ticks = 0
                target = "adaptive"
                reason = "high_load_or_unhealthy_server"
            elif old == "adaptive":
                self._auto_calm_ticks = self._auto_calm_ticks + 1 if calm else 0
                if self._auto_calm_ticks >= 6:
                    target = "sjf" if sjf_candidate else "static"
                    reason = "stable_mixed_jobs" if target == "sjf" else "stable_low_load"
                else:
                    target = "adaptive"
                    reason = "cooldown"
            else:
                self._auto_calm_ticks = 0
                self._auto_sjf_ticks = self._auto_sjf_ticks + 1 if sjf_candidate else 0
                if old == "sjf" and sjf_candidate:
                    target, reason = "sjf", "mixed_job_workload"
                elif old == "sjf" and not sjf_candidate:
                    # A few clean observations before leaving SJF, unless
                    # a high-load/failure condition triggered above.
                    self._auto_sjf_exit_ticks = getattr(self, "_auto_sjf_exit_ticks", 0) + 1
                    target = "static" if self._auto_sjf_exit_ticks >= 6 else "sjf"
                    reason = "uniform_jobs" if target == "static" else "sjf_exit_cooldown"
                else:
                    target = "sjf" if self._auto_sjf_ticks >= 2 else "static"
                    reason = "mixed_job_workload" if target == "sjf" else "normal_load"
                if sjf_candidate:
                    self._auto_sjf_exit_ticks = 0
        if target != old:
            self.lb.mode = target
            self._ev("mode_change", previous=old, mode=target,
                     requested_mode=self.requested_mode, reason=reason)

    def _log_overload_transitions(self):
        for s in self.lb.servers.values():
            current = self.lb.overloaded(s)
            previous = self._overload_state.get(s.name, False)
            if current != previous:
                self._ev("overload_detected" if current else "overload_cleared",
                         server=s.name, est_active=self.lb.est_active(s),
                         slots=s.slots, util=round(self.lb.util(s), 3))
                self._overload_state[s.name] = current

    def _server_down(self, s, reason):
        if self.lb.mode == "static":
            # baseline has no failure handling; we only record what happened
            self._ev("server_down_unhandled", server=s.name, reason=reason)
            return
        self.logger.warning("server %s DOWN (%s) - removing its flows", s.name, reason)
        self._ev("server_down", server=s.name, reason=reason)
        for k in [k for k, v in self.flows.items() if v == s.name]:
            del self.flows[k]
        if self.dp is None:
            return
        dp = self.dp
        ofp, p = dp.ofproto, dp.ofproto_parser
        # delete every flow that outputs to the dead server's port ...
        dp.send_msg(p.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE,
                                 out_port=s.port, out_group=ofp.OFPG_ANY,
                                 match=p.OFPMatch()))
        # ... and the reverse flows coming from it
        dp.send_msg(p.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE,
                                 out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                 match=p.OFPMatch(eth_type=0x0800, ipv4_src=s.ip)))
