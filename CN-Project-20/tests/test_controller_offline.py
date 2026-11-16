"""
test_controller_offline.py - exercises controller/adaptive_lb.py without a switch.

Uses the REAL packet library (os-ken or Ryu) and OpenFlow 1.3 message classes (every message
the controller sends is serialised, which validates match fields / actions),
but replaces Ryu's event loop (eventlet) with tiny stubs and the switch with a
fake datapath that just records messages.

Needs the Ryu package importable. If `import ryu` already works (normal install)
it is used; otherwise point RYU_SRC at an unpacked ryu source tree:
    RYU_SRC=/path/to/ryu-4.34 python3 -m unittest -v tests/test_controller_offline.py
"""
import json
import os
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.environ.get("RYU_SRC"):
    sys.path.insert(0, os.environ["RYU_SRC"])


PKG = None


def install_stubs(pkg):
    """Replace only the eventlet-dependent parts of the framework."""
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class RyuApp(object):
        def __init__(self, *a, **k):
            import logging
            self.logger = logging.getLogger("stub")

    class _Ev(object):
        pass

    mod(pkg + ".base")
    mod(pkg + ".base.app_manager", RyuApp=RyuApp)
    ofp_ev = mod(pkg + ".controller.ofp_event")
    for n in ("EventOFPSwitchFeatures", "EventOFPPacketIn", "EventOFPPortStatsReply",
              "EventOFPPortStatus"):
        setattr(ofp_ev, n, type(n, (_Ev,), {}))
    mod(pkg + ".controller.handler", CONFIG_DISPATCHER="config", MAIN_DISPATCHER="main",
        set_ev_cls=lambda *a, **k: (lambda f: f))
    ctrl = mod(pkg + ".controller", ofp_event=ofp_ev)
    ctrl.handler = sys.modules[pkg + ".controller.handler"]
    hub = mod(pkg + ".lib.hub", spawn=lambda *a, **k: None, sleep=lambda s: None)
    sys.modules[pkg + ".lib"].hub = hub


# 1) is a complete framework importable (needs eventlet etc.)?
for _pkg in ("os_ken", "ryu"):
    try:
        __import__(_pkg + ".base.app_manager")
        __import__(_pkg + ".controller.ofp_event")
        PKG, REAL = _pkg, True
        break
    except Exception:
        pass
# 2) otherwise use only the pure-python packet/OpenFlow libraries + stubs
if PKG is None:
    REAL = False
    for _pkg in ("os_ken", "ryu"):
        try:
            __import__(_pkg + ".lib.packet.packet")
            PKG = _pkg
            break
        except Exception:
            continue
    if PKG is None:
        raise unittest.SkipTest("neither os_ken nor ryu is importable "
                                "(run ./setup.sh or set RYU_SRC)")
    for k in [k for k in sys.modules
              if k.startswith((PKG + ".controller", PKG + ".base")) or k == PKG + ".lib.hub"]:
        del sys.modules[k]
    install_stubs(PKG)
os.environ["LB_BACKEND"] = PKG

import importlib  # noqa: E402
_m = lambda n: importlib.import_module(PKG + "." + n)  # noqa: E731
arp, ethernet, ether_types = _m("lib.packet.arp"), _m("lib.packet.ethernet"), _m("lib.packet.ether_types")
ipv4, packet, tcp, udp = (_m("lib.packet.ipv4"), _m("lib.packet.packet"),
                          _m("lib.packet.tcp"), _m("lib.packet.udp"))
ofp, parser = _m("ofproto.ofproto_v1_3"), _m("ofproto.ofproto_v1_3_parser")

os.environ["LB_LOG"] = os.path.join(tempfile.mkdtemp(), "ev.jsonl")
os.environ["LB_MODE"] = "adaptive"
sys.path.insert(0, os.path.join(ROOT, "controller"))
import adaptive_lb  # noqa: E402

VIP, VMAC, HB_IP = adaptive_lb.VIP, adaptive_lb.VMAC, adaptive_lb.HB_IP


class FakeDP(object):
    id = 1
    ofproto = ofp
    ofproto_parser = parser

    def __init__(self):
        self.sent = []

    def send_msg(self, m):
        m.serialize()            # raises if a match/action is malformed
        self.sent.append(m)


class FakeMsg(object):
    def __init__(self, dp, data, in_port):
        self.datapath, self.data = dp, data
        self.match = {"in_port": in_port}
        self.buffer_id = ofp.OFP_NO_BUFFER


def udp_hb(src_mac, src_ip, hb):
    p = packet.Packet()
    p.add_protocol(ethernet.ethernet(dst=VMAC, src=src_mac, ethertype=ether_types.ETH_TYPE_IP))
    p.add_protocol(ipv4.ipv4(src=src_ip, dst=HB_IP, proto=17))
    p.add_protocol(udp.udp(src_port=5555, dst_port=adaptive_lb.HB_PORT))
    p.add_protocol(json.dumps(hb).encode())
    p.serialize()
    return bytes(p.data)


def syn(src_mac, src_ip, sport, flags=0x02):
    p = packet.Packet()
    p.add_protocol(ethernet.ethernet(dst=VMAC, src=src_mac, ethertype=ether_types.ETH_TYPE_IP))
    p.add_protocol(ipv4.ipv4(src=src_ip, dst=VIP, proto=6))
    p.add_protocol(tcp.tcp(src_port=sport, dst_port=adaptive_lb.LB_TCP_PORT, bits=flags))
    p.serialize()
    return bytes(p.data)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.app = adaptive_lb.AdaptiveLB()
        self.dp = FakeDP()
        self.app.dp = self.dp
        self.servers = {"srv1": ("00:00:00:00:00:11", "10.0.0.11", 11),
                        "srv2": ("00:00:00:00:00:12", "10.0.0.12", 12),
                        "srv3": ("00:00:00:00:00:13", "10.0.0.13", 13)}
        for name, (mac, ip, port) in self.servers.items():
            hb = {"name": name, "port": 8000, "slots": 4, "active": 0,
                  "svc_ms": 100.0, "served": 0}
            self.app.on_packet_in(types.SimpleNamespace(
                msg=FakeMsg(self.dp, udp_hb(mac, ip, hb), port)))
        self.dp.sent.clear()

    def packet_in(self, data, port):
        self.app.on_packet_in(types.SimpleNamespace(msg=FakeMsg(self.dp, data, port)))

    def test_heartbeats_register_servers(self):
        self.assertEqual(sorted(self.app.lb.servers), ["srv1", "srv2", "srv3"])
        s = self.app.lb.servers["srv2"]
        self.assertEqual((s.ip, s.mac, s.port), ("10.0.0.12", "00:00:00:00:00:12", 12))
        self.assertTrue(all(x.alive for x in self.app.lb.servers.values()))

    def test_syn_installs_rewrite_flows_and_packet_out(self):
        self.packet_in(syn("00:00:00:00:00:01", "10.0.0.1", 40000), 1)
        mods = [m for m in self.dp.sent if isinstance(m, parser.OFPFlowMod)]
        outs = [m for m in self.dp.sent if isinstance(m, parser.OFPPacketOut)]
        self.assertEqual(len(mods), 2)
        self.assertEqual(len(outs), 1)
        fwd, rev = mods
        srv_name = self.app.flows[("10.0.0.1", 40000)]
        mac, ip, port = self.servers[srv_name]
        self.assertEqual(fwd.match["ipv4_dst"], VIP)
        self.assertEqual(fwd.match["tcp_src"], 40000)
        acts = fwd.instructions[0].actions
        self.assertEqual([(a.key, a.value) for a in acts[:2]],
                         [("eth_dst", mac), ("ipv4_dst", ip)])
        self.assertEqual(acts[2].port, port)
        self.assertEqual(rev.match["ipv4_src"], ip)
        self.assertEqual(rev.match["ipv4_dst"], "10.0.0.1")
        racts = rev.instructions[0].actions
        self.assertEqual([(a.key, a.value) for a in racts[:2]],
                         [("eth_src", VMAC), ("ipv4_src", VIP)])
        self.assertEqual(racts[2].port, 1)
        self.assertEqual(fwd.idle_timeout, self.app.flow_idle)

    def test_new_connections_are_spread_over_servers(self):
        for i in range(9):
            self.packet_in(syn("00:00:00:00:00:01", "10.0.0.1", 41000 + i), 1)
        names = [self.app.flows[("10.0.0.1", 41000 + i)] for i in range(9)]
        self.assertEqual({n: names.count(n) for n in set(names)},
                         {"srv1": 3, "srv2": 3, "srv3": 3})

    def test_dead_server_gets_no_new_flows_and_its_flows_are_deleted(self):
        self.packet_in(syn("00:00:00:00:00:01", "10.0.0.1", 42000), 1)
        self.dp.sent.clear()
        # srv2 stops sending heartbeats (its last one is now 10 s old)
        self.app.lb.servers["srv2"].last_hb -= 10
        dead = self.app.lb.expire(time.time())
        self.assertEqual([d.name for d in dead], ["srv2"])
        self.app._server_down(dead[0], "heartbeat_timeout")
        dels = [m for m in self.dp.sent if isinstance(m, parser.OFPFlowMod)]
        self.assertEqual(len(dels), 2)
        self.assertTrue(all(m.command == ofp.OFPFC_DELETE for m in dels))
        self.assertEqual(dels[0].out_port, 12)
        self.dp.sent.clear()
        for i in range(8):
            self.packet_in(syn("00:00:00:00:00:01", "10.0.0.1", 43000 + i), 1)
        used = {self.app.flows[("10.0.0.1", 43000 + i)] for i in range(8)}
        self.assertEqual(used, {"srv1", "srv3"})

    def test_arp_for_vip_is_answered_by_controller(self):
        p = packet.Packet()
        p.add_protocol(ethernet.ethernet(dst="ff:ff:ff:ff:ff:ff", src="00:00:00:00:00:01",
                                         ethertype=ether_types.ETH_TYPE_ARP))
        p.add_protocol(arp.arp(opcode=arp.ARP_REQUEST, src_mac="00:00:00:00:00:01",
                               src_ip="10.0.0.1", dst_mac="00:00:00:00:00:00", dst_ip=VIP))
        p.serialize()
        self.packet_in(bytes(p.data), 1)
        out = self.dp.sent[-1]
        self.assertIsInstance(out, parser.OFPPacketOut)
        reply = packet.Packet(out.data).get_protocol(arp.arp)
        self.assertEqual((reply.opcode, reply.src_mac, reply.src_ip),
                         (arp.ARP_REPLY, VMAC, VIP))
        self.assertEqual(out.actions[0].port, 1)

    def test_non_syn_without_state_is_dropped(self):
        self.packet_in(syn("00:00:00:00:00:01", "10.0.0.1", 44000, flags=0x10), 1)
        self.assertEqual(self.dp.sent, [])

    def test_non_syn_with_state_sticks_to_same_server(self):
        self.packet_in(syn("00:00:00:00:00:01", "10.0.0.1", 45000), 1)
        first = self.app.flows[("10.0.0.1", 45000)]
        self.dp.sent.clear()
        self.packet_in(syn("00:00:00:00:00:01", "10.0.0.1", 45000, flags=0x10), 1)
        self.assertEqual(self.app.flows[("10.0.0.1", 45000)], first)
        self.assertEqual(len([m for m in self.dp.sent if isinstance(m, parser.OFPFlowMod)]), 2)

    def test_heartbeat_is_not_forwarded(self):
        mac, ip, port = self.servers["srv1"]
        self.packet_in(udp_hb(mac, ip, {"name": "srv1", "active": 2, "slots": 4}), port)
        self.assertEqual(self.dp.sent, [])
        self.assertEqual(self.app.lb.servers["srv1"].active, 2)


    def test_runtime_mode_switch_logs_transition(self):
        with tempfile.TemporaryDirectory() as td:
            self.app.control_path = os.path.join(td, "control.json")
            with open(self.app.control_path, "w") as fh:
                json.dump({"mode": "static"}, fh)
            self.app._read_control_mode()
            self.assertEqual(self.app.lb.mode, "static")
            with open(os.environ["LB_LOG"]) as fh:
                events = [json.loads(x) for x in fh if x.strip()]
            self.assertTrue(any(e.get("ev") == "mode_change" and
                                e.get("previous") == "adaptive" and
                                e.get("mode") == "static" for e in events))

    def test_overload_transition_events_are_logged_once(self):
        srv = self.app.lb.servers["srv1"]
        srv.active = 6
        self.app._log_overload_transitions()
        self.app._log_overload_transitions()
        with open(os.environ["LB_LOG"]) as fh:
            events = [json.loads(x) for x in fh if x.strip()]
        detected = [e for e in events if e.get("ev") == "overload_detected"
                    and e.get("server") == "srv1"]
        self.assertEqual(len(detected), 1)
        srv.active = 0
        self.app._log_overload_transitions()
        with open(os.environ["LB_LOG"]) as fh:
            events = [json.loads(x) for x in fh if x.strip()]
        self.assertTrue(any(e.get("ev") == "overload_cleared" and
                            e.get("server") == "srv1" for e in events))


if __name__ == "__main__":
    unittest.main()
