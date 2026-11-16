"""
lb_policy.py - pure-Python load-balancing policy (no Ryu / no OpenFlow here).

Keeping the decision logic separate from the OpenFlow plumbing means it can be
unit-tested without Mininet and explained clearly in the viva.

Three modes
-----------
sjf      : healthy-server selection for the SJF worker-queue mode.
           The actual shortest-job-first dispatch is performed in app/server.py
           after reading the REQ work_ms field.
static   : plain round-robin over every server that has ever registered.
           It ignores load, speed, and health (the BASELINE for comparison).
adaptive : picks the live, non-overloaded server with the lowest score, where

               score = W_TIME * (expected_wait_ms / REF_MS) + W_NET * net_util

           expected_wait_ms = (est_active + 1) * svc_ms / slots
               est_active = connections the server reported + flows the
                            controller assigned since that report
               svc_ms     = server's recent service time (slow server => bigger)
               slots      = server's worker slots (its capacity)
           net_util = server-port throughput / link capacity (from OpenFlow
                      port statistics collected by the controller)

A server is dead if its heartbeat is older than hb_timeout or its switch port
is down. A server is overloaded if est_active / slots > overload_util.
"""
import time


class Server(object):
    def __init__(self, name, ip, mac, port):
        self.name = name
        self.ip = ip              # IP learned from the heartbeat packet
        self.mac = mac            # MAC learned from the heartbeat packet
        self.port = port          # switch port learned from the packet-in
        self.tcp_port = 8000
        self.slots = 4
        self.svc_ms = 100.0
        self.active = 0           # in-flight requests reported by the server
        self.served = 0
        self.last_hb = 0.0
        self.assigned = 0         # flows assigned since the last heartbeat
        self.total_assigned = 0
        self.net_bps = 0.0        # smoothed throughput of the server's port
        self.link_bps = 10e6
        self.port_down = False
        self.alive = False        # last evaluated liveness (for transitions)
        self._last_rx = None
        self._last_rx_t = None
        self.job_mix = False
        self.job_samples = 0
        self.job_min_ms = 0.0
        self.job_max_ms = 0.0
        self.pending_jobs = 0
        self.sjf_dispatched = 0
        self.fifo_dispatched = 0
        self.last_job_ms = 0.0


class LoadBalancer(object):
    def __init__(self, mode="adaptive", w_time=0.7, w_net=0.3, ref_ms=100.0,
                 overload_util=1.25, hb_timeout=2.0, link_bps=10e6):
        if mode not in ("adaptive", "static", "sjf"):
            raise ValueError("mode must be 'adaptive', 'static' or 'sjf'")
        self.mode = mode
        self.w_time = w_time
        self.w_net = w_net
        self.ref_ms = ref_ms
        self.overload_util = overload_util
        self.hb_timeout = hb_timeout
        self.link_bps = link_bps
        self.servers = {}
        self._rr = 0

    # ------------------------------------------------------------ inputs
    def on_heartbeat(self, hb, ip, mac, port, now=None):
        """Process one heartbeat. Returns (server, came_up)."""
        now = time.time() if now is None else now
        name = str(hb["name"])
        s = self.servers.get(name)
        if s is None:
            s = Server(name, ip, mac, port)
            s.link_bps = self.link_bps
            self.servers[name] = s
        s.ip, s.mac, s.port = ip, mac, port
        s.tcp_port = int(hb.get("port", s.tcp_port))
        s.slots = max(1, int(hb.get("slots", s.slots)))
        s.svc_ms = max(1.0, float(hb.get("svc_ms", s.svc_ms)))
        s.active = int(hb.get("active", 0))
        s.served = int(hb.get("served", 0))
        s.job_mix = bool(hb.get("job_mix", False))
        s.job_samples = int(hb.get("job_samples", 0))
        s.job_min_ms = float(hb.get("job_min_ms", 0))
        s.job_max_ms = float(hb.get("job_max_ms", 0))
        s.pending_jobs = int(hb.get("pending_jobs", 0))
        s.sjf_dispatched = int(hb.get("sjf_dispatched", 0))
        s.fifo_dispatched = int(hb.get("fifo_dispatched", 0))
        s.last_job_ms = float(hb.get("last_job_ms", 0))
        s.assigned = 0            # the report now includes our recent flows
        s.last_hb = now
        s.port_down = False       # a heartbeat proves the link works
        was = s.alive
        s.alive = self._alive(s, now)
        return s, (s.alive and not was)

    def update_net(self, port, rx_bytes, now=None):
        """rx_bytes = bytes the switch received from this server's port
        (the response direction, which carries the heavy traffic)."""
        now = time.time() if now is None else now
        for s in self.servers.values():
            if s.port != port:
                continue
            if s._last_rx is not None and now > s._last_rx_t:
                rate = (rx_bytes - s._last_rx) * 8.0 / (now - s._last_rx_t)
                rate = max(rate, 0.0)
                s.net_bps = rate if s.net_bps == 0 else 0.5 * s.net_bps + 0.5 * rate
            s._last_rx, s._last_rx_t = rx_bytes, now

    def set_port_down(self, port, down, now=None):
        """Returns (server, changed) or (None, False)."""
        now = time.time() if now is None else now
        for s in self.servers.values():
            if s.port == port:
                s.port_down = down
                a = self._alive(s, now)
                changed = (a != s.alive)
                s.alive = a
                return s, changed
        return None, False

    def expire(self, now=None):
        """Return servers that just transitioned alive -> dead."""
        now = time.time() if now is None else now
        dead = []
        for s in self.servers.values():
            a = self._alive(s, now)
            if s.alive and not a:
                dead.append(s)
            s.alive = a
        return dead

    # ------------------------------------------------------------ metrics
    def _alive(self, s, now):
        return (now - s.last_hb) <= self.hb_timeout and not s.port_down

    def is_alive(self, s, now=None):
        return self._alive(s, time.time() if now is None else now)

    def est_active(self, s):
        return s.active + s.assigned

    def est_wait_ms(self, s):
        return (self.est_active(s) + 1) * s.svc_ms / float(s.slots)

    def util(self, s):
        return self.est_active(s) / float(s.slots)

    def net_util(self, s):
        return min(s.net_bps / s.link_bps, 2.0) if s.link_bps else 0.0

    def overloaded(self, s):
        return self.util(s) > self.overload_util

    def score(self, s):
        return (self.w_time * self.est_wait_ms(s) / self.ref_ms
                + self.w_net * self.net_util(s))

    # ------------------------------------------------------------ decision
    def pick(self, now=None):
        """Choose a server for a NEW connection. Returns Server or None."""
        now = time.time() if now is None else now
        if not self.servers:
            return None
        if self.mode == "static":
            names = sorted(self.servers)
            s = self.servers[names[self._rr % len(names)]]
            self._rr += 1
        else:
            cands = [s for s in self.servers.values() if self._alive(s, now)]
            if not cands:
                return None
            ok = [s for s in cands if not self.overloaded(s)]
            pool = ok or cands    # if everything is overloaded, still pick best
            if self.mode == "sjf":
                # SJF work ordering happens inside the server, after the REQ
                # body is received. Connection routing uses healthy/available
                # capacity, not a false claim to know job sizes at TCP SYN.
                s = min(pool, key=lambda x: (self.util(x), self.est_wait_ms(x), x.name))
            else:
                s = min(pool, key=lambda x: (self.score(x), x.assigned, x.name))
        s.assigned += 1
        s.total_assigned += 1
        return s

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        out = []
        for name in sorted(self.servers):
            s = self.servers[name]
            out.append({
                "name": s.name,
                "alive": self._alive(s, now),
                "active": s.active,
                "served": s.served,
                "est_active": self.est_active(s),
                "slots": s.slots,
                "svc_ms": round(s.svc_ms, 1),
                "util": round(self.util(s), 3),
                "net_util": round(self.net_util(s), 3),
                "score": round(self.score(s), 3),
                "overloaded": self.overloaded(s),
                "job_mix": s.job_mix,
                "job_samples": s.job_samples,
                "job_min_ms": s.job_min_ms,
                "job_max_ms": s.job_max_ms,
                "pending_jobs": s.pending_jobs,
                "sjf_dispatched": s.sjf_dispatched,
                "fifo_dispatched": s.fifo_dispatched,
                "last_job_ms": s.last_job_ms,
                "total_assigned": s.total_assigned,
            })
        return out
