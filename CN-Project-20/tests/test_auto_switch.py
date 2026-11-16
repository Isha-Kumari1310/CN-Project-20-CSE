import json, sys, time, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"controller"))

# Offline tests of the actual controller method, with a minimal fake instance.
from adaptive_lb import AdaptiveLB
from lb_policy import LoadBalancer

class AutoSwitchTests(unittest.TestCase):
    def setUp(self):
        self.obj=object.__new__(AdaptiveLB)
        self.obj.requested_mode="auto"
        self.obj._auto_calm_ticks=0
        self.obj.control_path="/file/that/does/not/exist"
        self.obj.lb=LoadBalancer(mode="static",hb_timeout=2)
        self.log=[]
        self.obj._ev=lambda event,**fields:self.log.append((event,fields))
        now=time.time()
        for i in range(3):
            s,_=self.obj.lb.on_heartbeat(dict(name=f"srv{i}",slots=4,active=0,svc_ms=100),"10.0.0.1","aa:bb:cc:dd:ee:01",i+1,now)
    def test_stress_and_cooldown(self):
        now=time.time()
        self.obj._read_control_mode(now)
        self.assertEqual(self.obj.lb.mode,"static")
        self.obj.lb.servers['srv0'].active=4
        self.obj._read_control_mode(now)
        self.assertEqual(self.obj.lb.mode,"adaptive")
        self.obj.lb.servers['srv0'].active=0
        for _ in range(6):self.obj._read_control_mode(now)
        self.assertEqual(self.obj.lb.mode,"static")
        self.assertEqual(len(self.log),2)
    def test_dead_server_triggers_adaptive(self):
        now=time.time()
        self.obj.lb.servers['srv0'].last_hb=now-10
        self.obj._read_control_mode(now)
        self.assertEqual(self.obj.lb.mode,"adaptive")
if __name__=='__main__':unittest.main()
