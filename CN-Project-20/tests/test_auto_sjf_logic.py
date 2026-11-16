"""Exercise actual AUTO decision method without requiring OS-Ken installation.

This loads the method's AST from the source rather than importing the
OpenFlow framework, so it can run during packaging on non-Mininet hosts.
"""
import ast
import sys
import time
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'controller'))
from lb_policy import LoadBalancer
source = (ROOT / 'controller/adaptive_lb.py').read_text()
module = ast.parse(source)
cls = next(x for x in module.body if isinstance(x, ast.ClassDef) and x.name == 'AdaptiveLB')
method = next(x for x in cls.body if isinstance(x, ast.FunctionDef) and x.name == '_read_control_mode')
code = compile(ast.Module(body=[method], type_ignores=[]), '<actual_auto_controller_method>', 'exec')
scope = {'time': time, 'json': __import__('json')}
exec(code, scope)

class Dummy:
    _read_control_mode = scope['_read_control_mode']
    def __init__(self):
        self.requested_mode = 'auto'
        self.control_path = '/no/such/control/file.json'
        self._auto_calm_ticks = 0
        self._auto_sjf_ticks = 0
        self.lb = LoadBalancer(mode='static')
        self.events = []
        self._ev = lambda name, **fields: self.events.append((name, fields))
        self.now = time.time()
        for i in range(3):
            self.lb.on_heartbeat({'name':f'srv{i}', 'slots':4,'active':0,'svc_ms':100},
                                 f'10.0.0.{11+i}',f'00:00:00:00:00:{11+i:02x}',i+1, self.now)
    def check(self, repeats=1):
        for _ in range(repeats):
            self._read_control_mode(self.now)

class AutoSjfTests(unittest.TestCase):
    def test_normal_stays_rr(self):
        o=Dummy();o.check(10);self.assertEqual(o.lb.mode,'static')

    def test_mixed_job_sizes_activate_sjf(self):
        o=Dummy();s=o.lb.servers['srv0'];s.active=1;s.job_mix=True;s.job_samples=12
        o.check(2);self.assertEqual(o.lb.mode,'sjf')

    def test_overload_overrides_sjf_and_cools_down(self):
        o=Dummy();s=o.lb.servers['srv0'];s.active=1;s.job_mix=True;s.job_samples=12
        o.check(2);self.assertEqual(o.lb.mode,'sjf')
        s.active=4;o.check();self.assertEqual(o.lb.mode,'adaptive')
        s.active=0;o.check(6);self.assertEqual(o.lb.mode,'static')

    def test_unresponsive_overrides_sjf(self):
        o=Dummy();s=o.lb.servers['srv0'];s.active=1;s.job_mix=True;s.job_samples=12
        o.check(2)
        s.last_hb=o.now-5;o.check();self.assertEqual(o.lb.mode,'adaptive')

if __name__=='__main__':unittest.main()
