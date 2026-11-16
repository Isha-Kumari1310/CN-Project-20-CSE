import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "controller"))
from server import pending_index, current_mode
from lb_policy import LoadBalancer


class SJFTests(unittest.TestCase):
    def test_sjf_shortest_job_first(self):
        jobs = [(1, None, "long", 500, 1), (2, None, "short", 25, 1),
                (3, None, "medium", 100, 1)]
        self.assertEqual([jobs.pop(pending_index(jobs, "sjf"))[2] for _ in range(3)],
                         ["short", "medium", "long"])

    def test_sjf_is_stable_for_equal_length(self):
        jobs = [(3, None, "third", 50, 1), (1, None, "first", 50, 1)]
        self.assertEqual(jobs[pending_index(jobs, "sjf")][2], "first")

    def test_rr_and_adaptive_use_fifo_server_dispatch(self):
        jobs = [(1, None, "long", 500, 1), (2, None, "short", 25, 1)]
        for mode in ("static", "adaptive"):
            self.assertEqual(jobs[pending_index(jobs, mode)][2], "long")

    def test_sjf_policy_skips_dead_server(self):
        lb = LoadBalancer(mode="sjf")
        for name, ip in [("srv1", "10.0.0.11"), ("srv2", "10.0.0.12")]:
            lb.on_heartbeat({"name": name, "slots": 4, "active": 0}, ip,
                            "00:00:00:00:00:01", len(lb.servers)+1, now=100)
        lb.servers["srv1"].port_down = True
        self.assertEqual(lb.pick(now=100).name, "srv2")

    def test_current_mode_from_controller_state(self):
        import os
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(json.dumps({"t": time.time(), "effective_mode": "sjf"}))
            prev = os.environ.get("LB_STATUS")
            try:
                os.environ["LB_STATUS"] = str(path)
                self.assertEqual(current_mode(), "sjf")
            finally:
                if prev is None:
                    del os.environ["LB_STATUS"]
                else:
                    os.environ["LB_STATUS"] = prev

if __name__ == "__main__":
    unittest.main()
