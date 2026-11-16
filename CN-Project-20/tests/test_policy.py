"""Unit tests for controller/lb_policy.py  (run: python3 -m unittest -v tests/test_policy.py)"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "controller"))
from lb_policy import LoadBalancer  # noqa: E402


def hb(name, active=0, slots=4, svc=100.0, port=8000):
    return {"name": name, "active": active, "slots": slots, "svc_ms": svc,
            "port": port, "served": 0}


def make(mode="adaptive", **kw):
    lb = LoadBalancer(mode=mode, **kw)
    for i, n in enumerate(["srv1", "srv2", "srv3"], 1):
        lb.on_heartbeat(hb(n), "10.0.0.%d" % (10 + i), "mac%d" % i, i + 10, now=100.0)
    return lb


class PolicyTests(unittest.TestCase):
    def test_static_is_round_robin_and_ignores_load(self):
        lb = make("static")
        lb.on_heartbeat(hb("srv1", active=50), "10.0.0.11", "mac1", 11, now=100.0)
        order = [lb.pick(100.0).name for _ in range(6)]
        self.assertEqual(order, ["srv1", "srv2", "srv3"] * 2)

    def test_static_keeps_dead_server_in_rotation(self):
        lb = make("static")
        self.assertEqual(len(lb.expire(200.0)), 3)         # all heartbeats stale
        names = {lb.pick(200.0).name for _ in range(3)}
        self.assertEqual(names, {"srv1", "srv2", "srv3"})

    def test_adaptive_prefers_least_loaded(self):
        lb = make()
        lb.on_heartbeat(hb("srv1", active=3), "10.0.0.11", "mac1", 11, now=100.0)
        lb.on_heartbeat(hb("srv2", active=0), "10.0.0.12", "mac2", 12, now=100.0)
        lb.on_heartbeat(hb("srv3", active=2), "10.0.0.13", "mac3", 13, now=100.0)
        self.assertEqual(lb.pick(100.0).name, "srv2")

    def test_adaptive_accounts_for_slow_server(self):
        lb = make()
        # srv3 is 2x slower -> same connection count means a longer wait
        lb.on_heartbeat(hb("srv3", active=1, svc=200.0), "10.0.0.13", "mac3", 13, now=100.0)
        picks = [lb.pick(100.0).name for _ in range(9)]
        self.assertLess(picks.count("srv3"), picks.count("srv1"))

    def test_between_heartbeats_flows_are_spread(self):
        lb = make()
        picks = [lb.pick(100.0).name for _ in range(9)]
        self.assertEqual({p: picks.count(p) for p in set(picks)},
                         {"srv1": 3, "srv2": 3, "srv3": 3})

    def test_heartbeat_resets_assigned(self):
        lb = make()
        for _ in range(5):
            lb.pick(100.0)
        s, _ = lb.on_heartbeat(hb("srv1"), "10.0.0.11", "mac1", 11, now=100.5)
        self.assertEqual(s.assigned, 0)

    def test_overloaded_server_is_excluded(self):
        lb = make()
        lb.on_heartbeat(hb("srv1", active=10), "10.0.0.11", "mac1", 11, now=100.0)
        self.assertTrue(lb.overloaded(lb.servers["srv1"]))
        picks = {lb.pick(100.0).name for _ in range(6)}
        self.assertNotIn("srv1", picks)

    def test_all_overloaded_still_picks_best(self):
        lb = make()
        for i, n in enumerate(["srv1", "srv2", "srv3"], 1):
            lb.on_heartbeat(hb(n, active=10 + i), "10.0.0.%d" % (10 + i), "m", i + 10, now=100.0)
        self.assertEqual(lb.pick(100.0).name, "srv1")

    def test_dead_server_detected_and_skipped(self):
        lb = make(hb_timeout=2.0)
        lb.on_heartbeat(hb("srv1"), "10.0.0.11", "mac1", 11, now=103.0)
        lb.on_heartbeat(hb("srv3"), "10.0.0.13", "mac3", 13, now=103.0)
        dead = lb.expire(103.5)                           # srv2 silent for 3.5 s
        self.assertEqual([s.name for s in dead], ["srv2"])
        self.assertEqual(lb.expire(103.6), [])            # reported once only
        self.assertNotIn("srv2", {lb.pick(103.6).name for _ in range(10)})

    def test_server_recovers_after_heartbeat(self):
        lb = make(hb_timeout=2.0)
        lb.expire(110.0)
        s, up = lb.on_heartbeat(hb("srv2"), "10.0.0.12", "mac2", 12, now=110.1)
        self.assertTrue(up)
        self.assertTrue(lb.is_alive(s, 110.1))

    def test_port_down_marks_dead_immediately(self):
        lb = make()
        s, changed = lb.set_port_down(12, True, now=100.1)
        self.assertEqual(s.name, "srv2")
        self.assertTrue(changed)
        self.assertNotIn("srv2", {lb.pick(100.1).name for _ in range(10)})

    def test_no_live_server_returns_none(self):
        lb = make()
        lb.expire(500.0)
        self.assertIsNone(lb.pick(500.0))

    def test_network_load_steers_traffic(self):
        lb = make()
        lb.update_net(11, 0, now=100.0)
        lb.update_net(11, 1_000_000, now=101.0)           # 8 Mbit/s on a 10 Mbit/s link
        self.assertAlmostEqual(lb.servers["srv1"].net_bps, 8e6, delta=1)
        self.assertEqual(lb.pick(101.0).name, "srv2")     # srv1's link is busy

    def test_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            LoadBalancer(mode="nonsense")


if __name__ == "__main__":
    unittest.main()
