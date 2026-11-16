#!/usr/bin/env python3
"""Local live dashboard for the SDN adaptive load balancer. Run as root."""
import csv, json, os, signal, socket, subprocess, sys, threading, time, traceback
from collections import deque
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT,"topology"))
import lb_topo
HOST="0.0.0.0"; PORT=8080
EVENT_LOG=os.environ.get("LB_LOG","/tmp/lb_events.jsonl")
CONTROL="/tmp/lb_control.json"
STATUS="/tmp/lb_effective_mode.json"
lock=threading.RLock()
routing={"offset":0,"file_id":None,"partial":b"","counts":{mode:{name:0 for name in ("srv1","srv2","srv3")} for mode in ("static","sjf","adaptive")},"recent":deque(maxlen=40)}
routing_lock=threading.Lock()
runtime={"controller":None,"net":None,"servers":{},"clients":[],"client_files":[],
         "mode":"auto","started_at":None,"errors":[],"injected":{}}
def port_open(port):
    s=socket.socket(); s.settimeout(.25)
    try: s.connect(("127.0.0.1",port)); return True
    except OSError: return False
def event_tail(limit=250):
    out=[]
    try:
        with open(EVENT_LOG,errors="replace") as f:
            lines=f.readlines()[-limit:]
        for line in lines:
            try: out.append(json.loads(line))
            except ValueError: pass
    except OSError: pass
    return out
def reset_routing():
    with routing_lock:
        routing["offset"] = 0
        routing["file_id"] = None
        routing["partial"] = b""
        routing["counts"] = {mode:{name:0 for name in ("srv1","srv2","srv3")}
                             for mode in ("static","sjf","adaptive")}
        routing["recent"].clear()

def routing_snapshot():
    """Accumulate real OpenFlow assignment events once per controller run.

    This counts NEW TCP flow assignments, not completed application requests.
    A partial final JSONL line is kept until the controller finishes writing it.
    """
    with routing_lock:
        try:
            st = os.stat(EVENT_LOG)
            identity = (st.st_dev, st.st_ino)
            if routing["file_id"] != identity or st.st_size < routing["offset"]:
                reset = {mode:{name:0 for name in ("srv1","srv2","srv3")}
                         for mode in ("static","sjf","adaptive")}
                routing.update(offset=0, file_id=identity, partial=b"", counts=reset)
                routing["recent"].clear()
            with open(EVENT_LOG, "rb") as fh:
                fh.seek(routing["offset"])
                chunk=fh.read()
                routing["offset"]=fh.tell()
            lines=(routing["partial"]+chunk).split(b"\n")
            routing["partial"]=lines.pop()
            for raw in lines:
                try: event=json.loads(raw)
                except (ValueError, UnicodeDecodeError): continue
                if event.get("ev") != "assign": continue
                name, mode=event.get("server"),event.get("mode")
                if mode not in routing["counts"] or name not in routing["counts"][mode]: continue
                routing["counts"][mode][name]+=1
                routing["recent"].appendleft({
                    "t":event.get("t"),"client":event.get("client"),
                    "server":name,"mode":mode,"score":event.get("score")})
        except OSError:
            pass
        return {"counts":{m:dict(v) for m,v in routing["counts"].items()},
                "recent":list(routing["recent"])}

def latest_effective_mode():
    """Read the controller's current state instead of guessing from recent events.
    Avoid showing an old mode if the controller's heartbeat is stale."""
    try:
        with open(STATUS) as fh:
            state=json.load(fh)
        if time.time()-float(state.get("t", 0)) <= 3.0:
            return state.get("effective_mode")
    except (OSError, ValueError, TypeError):
        pass
    return None

def state_data():
    events=event_tail(500)
    snap=next((e.get("servers",[]) for e in reversed(events) if e.get("ev")=="state"),[])
    assignments=[e for e in events if e.get("ev")=="assign"][-80:]
    counts={n:0 for n in ("srv1","srv2","srv3")}
    for e in [x for x in events if x.get("ev")=="assign"]:
        if e.get("server") in counts: counts[e["server"]]+=1
    metrics={"ok":0,"failed":0,"retried":0,"req_s":0,"mean_ms":0,"p95_ms":0}
    lat=[]; rows=[]
    for fn in list(runtime["client_files"]):
        try:
            with open(fn,newline="") as f:
                for r in csv.DictReader(f):
                    rows.append(r)
        except (OSError,csv.Error): pass
    now=time.time()
    recent=[]
    for r in rows:
        try:
            t=float(r["t_start"]); latency=float(r["latency_ms"]); ok=int(r["ok"]); attempts=int(r["attempts"])
            metrics["ok"]+=ok; metrics["failed"]+=1-ok; metrics["retried"]+=attempts>1
            lat.append(latency)
            if t >= now-1: recent.append(t)
        except (KeyError,ValueError): pass
    if lat:
        lat.sort(); metrics["mean_ms"]=round(sum(lat)/len(lat),2)
        metrics["p95_ms"]=round(lat[min(len(lat)-1,int(.95*(len(lat)-1)))],2)
    metrics["req_s"]=len(recent)
    metrics["total"]=len(rows)
    effective=latest_effective_mode()
    if effective is None:
        effective=next((e.get("effective_mode") for e in reversed(events) if e.get("ev")=="start"), "static" if runtime["mode"]=="auto" else runtime["mode"])
    completed_by_server={name:0 for name in ("srv1","srv2","srv3")}
    for row in rows:
        if row.get("ok")=="1" and row.get("server") in completed_by_server:
            completed_by_server[row["server"]]+=1
    return {"routing":routing_snapshot(),"completed_by_server":completed_by_server,
            "running":runtime["net"] is not None,"controller":runtime["controller"] is not None and runtime["controller"].poll() is None,
            "switch":any(e.get("ev")=="switch_connected" for e in events),"mode":runtime["mode"],
            "effective_mode":effective,
            "mode_changes":[e for e in events if e.get("ev")=="mode_change"][::-1][:40],
            "servers":snap,"assignments":assignments[::-1],"counts":counts,"metrics":metrics,
            "events":events[::-1],"errors":runtime["errors"][-5:],
            "injected":runtime["injected"],"uptime":round(now-runtime["started_at"],1) if runtime["started_at"] else 0}
def cleanup():
    with lock:
        if runtime["injected"].get("unresponsive"):
            p=runtime["servers"].get("srv2")
            if p and p.poll() is None:
                try: os.kill(p.pid, signal.SIGCONT)
                except ProcessLookupError: pass
            runtime["injected"].pop("unresponsive",None)
        for p in runtime["clients"]:
            try: p.terminate()
            except Exception: pass
        runtime["clients"]=[]
        for p in runtime["servers"].values():
            try: p.terminate()
            except Exception: pass
        runtime["servers"]={}
        if runtime["net"] is not None:
            try: runtime["net"].stop()
            except Exception: pass
            runtime["net"]=None
        p=runtime["controller"]
        if p is not None:
            try: p.terminate(); p.wait(timeout=3)
            except Exception:
                try: p.kill()
                except Exception: pass
            runtime["controller"]=None
        try: subprocess.run(["mn","-c"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8)
        except Exception: pass
def start_network():
    with lock:
        if runtime["net"] is not None: return {"ok":True,"message":"Network already running"}
        if os.geteuid()!=0: raise RuntimeError("Start UI with sudo: sudo .venv/bin/python ui/app.py")
        if port_open(6653): raise RuntimeError("TCP port 6653 is already in use. Stop the old controller: sudo fuser -k 6653/tcp")
        os.makedirs("/tmp",exist_ok=True)
        # Remove stale files first: Ubuntu fs.protected_regular may prevent
        # root truncating a file owned by another user in sticky /tmp.
        for path in (EVENT_LOG, CONTROL, STATUS):
            try: os.unlink(path)
            except FileNotFoundError: pass
        open(EVENT_LOG,"w").close()
        reset_routing()
        with open(CONTROL,"w") as f: json.dump({"mode":runtime["mode"]},f)
        env=os.environ.copy(); env["LB_LOG"]=EVENT_LOG; env["LB_CONTROL"]=CONTROL; env["LB_STATUS"]=STATUS; env["LB_MODE"]=runtime["mode"]
        runtime["controller"]=subprocess.Popen([sys.executable,os.path.join(ROOT,"controller/run_controller.py"),"--mode",runtime["mode"],"--log",EVENT_LOG,"--port","6653"],cwd=ROOT,env=env,stdout=open("/tmp/lb_ui_controller.log","w"),stderr=subprocess.STDOUT)
        for _ in range(60):
            if runtime["controller"].poll() is not None: raise RuntimeError("Controller exited. Check /tmp/lb_ui_controller.log")
            if port_open(6653): break
            time.sleep(.25)
        else: raise RuntimeError("Controller did not listen on port 6653; check /tmp/lb_ui_controller.log")
        runtime["net"]=lb_topo.build_net()
        runtime["servers"]=lb_topo.start_servers(runtime["net"],"/tmp/lb_ui_servers")
        runtime["started_at"]=time.time(); runtime["injected"]={}
        return {"ok":True,"message":"Controller, switch, and servers started"}
def start_load(n=10,duration=3600,job_mix="uniform"):
    if job_mix not in ("uniform", "mixed"):
        raise ValueError("Invalid job mix")
    with lock:
        if runtime["net"] is None: raise RuntimeError("Start network first")
        for p in runtime["clients"]:
            if p.poll() is None: p.terminate()
        runtime["clients"]=[]; runtime["client_files"]=[]
        n=max(1,min(int(n),50)); per=[n//4+(1 if i<n%4 else 0) for i in range(4)]
        for i,count in enumerate(per,1):
            if count<=0: continue
            name="c%d"%i; fn="/tmp/lb_ui_%s.csv"%name
            try: os.remove(fn)
            except OSError: pass
            p=runtime["net"].get(name).popen(["python3","-u",os.path.join(ROOT,"app/client.py"),"--name",name,
                "--duration",str(duration),"--threads",str(count),"--work-ms","100","--resp-bytes","20000",
                "--think-ms","20","--job-mix",job_mix,"--out",fn],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            runtime["clients"].append(p); runtime["client_files"].append(fn)
        return {"ok":True,"message":"Started %d concurrent client loops (%s jobs)"%(n, job_mix)}
def action(name,data):
    with lock:
        net=runtime["net"]
        if name=="start": return start_network()
        if name=="stop": cleanup(); return {"ok":True,"message":"Network stopped and Mininet cleaned"}
        if name=="load": return start_load(data.get("clients",10),data.get("duration",3600),data.get("job_mix","uniform"))
        if name=="stop_load":
            for p in runtime["clients"]:
                try: p.terminate()
                except Exception: pass
            runtime["clients"]=[]; return {"ok":True,"message":"Load stopped"}
        if name=="mode":
            mode=data.get("mode")
            if mode not in ("static","adaptive","sjf","auto"): raise ValueError("Mode must be static, adaptive, sjf or auto")
            runtime["mode"]=mode
            with open(CONTROL,"w") as f: json.dump({"mode":mode},f)
            return {"ok":True,"message":"Mode switch requested: "+mode}
        if net is None: raise RuntimeError("Start network first")
        if name=="overload":
            p=runtime["servers"].get("srv1")
            if p and p.poll() is None: os.kill(p.pid,signal.SIGUSR1)
            runtime["injected"]["overload"]=time.time(); return {"ok":True,"message":"Toggled srv1 degraded mode"}
        if name=="clear_overload":
            # SIGUSR1 toggles, so clear only if the controller currently sees overload.
            snap=next((e.get("servers",[]) for e in reversed(event_tail()) if e.get("ev")=="state"),[])
            s=next((x for x in snap if x.get("name")=="srv1"),{})
            if s.get("overloaded"): 
                p=runtime["servers"].get("srv1")
                if p and p.poll() is None: os.kill(p.pid,signal.SIGUSR1)
            runtime["injected"].pop("overload",None); return {"ok":True,"message":"Clear overload toggle sent if currently overloaded"}
        if name=="unresponsive":
            p=runtime["servers"].get("srv2")
            if not p or p.poll() is not None: raise RuntimeError("srv2 is not running")
            if runtime["injected"].get("unresponsive"): return {"ok":True,"message":"srv2 already paused"}
            os.kill(p.pid,signal.SIGSTOP)
            runtime["injected"]["unresponsive"]=time.time()
            return {"ok":True,"message":"Paused srv2 (SIGSTOP). Heartbeats should time out; no process was killed."}
        if name=="responsive":
            p=runtime["servers"].get("srv2")
            if not p or p.poll() is not None: raise RuntimeError("srv2 is not running; use Recover Server 2")
            os.kill(p.pid,signal.SIGCONT)
            runtime["injected"].pop("unresponsive",None)
            return {"ok":True,"message":"Resumed srv2 (SIGCONT). Wait for a new heartbeat."}
        if name=="crash":
            p=runtime["servers"].get("srv2")
            if p and p.poll() is None: p.kill()
            runtime["injected"].pop("unresponsive",None)
            runtime["injected"]["crash"]=time.time(); return {"ok":True,"message":"srv2 process killed"}
        if name=="recover":
            if runtime["injected"].get("unresponsive"):
                p0=runtime["servers"].get("srv2")
                if p0 and p0.poll() is None: os.kill(p0.pid,signal.SIGCONT)
                runtime["injected"].pop("unresponsive",None)
            p=runtime["servers"].get("srv2")
            if p and p.poll() is None: p.terminate()
            try: p.wait(timeout=1)
            except Exception: pass
            runtime["servers"]["srv2"]=net.get("srv2").popen(["python3","-u",os.path.join(ROOT,"app/server.py"),"--name","srv2","--port","8000","--slots","4","--speed","1.0","--hb-ip",lb_topo.HB_IP,"--hb-interval","0.5"],stdout=open("/tmp/lb_ui_srv2.log","w"),stderr=subprocess.STDOUT)
            return {"ok":True,"message":"srv2 restarted; wait for server_up heartbeat"}
        if name=="link_down":
            net.configLinkStatus("srv2","s1","down"); runtime["injected"]["linkfail"]=time.time()
            return {"ok":True,"message":"Link srv2-s1 down"}
        if name=="link_up":
            net.configLinkStatus("srv2","s1","up"); return {"ok":True,"message":"Link srv2-s1 up"}
        if name=="flows":
            r=subprocess.run(["ovs-ofctl","-O","OpenFlow13","dump-flows","s1"],capture_output=True,text=True,timeout=5)
            return {"ok":r.returncode==0,"output":r.stdout or r.stderr}
        if name=="scenario":
            # Live scenario is intentionally explicit rather than silently faking results.
            scenario=data.get("scenario","normal")
            if scenario not in ("normal","overload","crash","linkfail"): raise ValueError("Unknown scenario")
            start_load(data.get("clients",10),30)
            if scenario!="normal":
                def inject():
                    time.sleep(10)
                    try: action({"overload":"overload","crash":"crash","linkfail":"link_down"}[scenario],{})
                    except Exception: pass
                threading.Thread(target=inject,daemon=True).start()
            return {"ok":True,"message":"Started real 30 s %s workload; event injection at 10 s"%scenario}
        raise ValueError("Unknown action: "+name)
class Handler(SimpleHTTPRequestHandler):
    def log_message(self,*a): pass
    def _json(self,obj,code=200):
        b=json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/api/state": return self._json(state_data())
        if path=="/api/events":
            self.send_response(200); self.send_header("Content-Type","text/event-stream"); self.send_header("Cache-Control","no-cache"); self.send_header("Connection","keep-alive"); self.end_headers()
            last=""
            try:
                while True:
                    state=state_data(); payload=json.dumps(state)
                    if payload!=last:
                        self.wfile.write(("data: "+payload+"\n\n").encode()); self.wfile.flush(); last=payload
                    time.sleep(1)
            except (BrokenPipeError,ConnectionResetError): pass
            return
        if path=="/api/summary":
            for fn in ("results/summary.csv","results_local_sim/summary.csv"):
                full=os.path.join(ROOT,fn)
                if os.path.isfile(full):
                    with open(full,errors="replace") as f: return self._json({"path":fn,"csv":f.read()})
            return self._json({"path":None,"csv":""})
        if path=="/": self.path="/ui/static/index.html"
        return super().do_GET()
    def do_POST(self):
        try:
            n=int(self.headers.get("Content-Length","0")); data=json.loads(self.rfile.read(n) or b"{}")
            result=action(urlparse(self.path).path.split("/")[-1],data); self._json(result)
        except Exception as e:
            runtime["errors"].append(str(e)); self._json({"ok":False,"error":str(e)},400)
if __name__=="__main__":
    os.chdir(ROOT)
    print("SDN dashboard: http://localhost:8080",flush=True)
    try: ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
    except KeyboardInterrupt: pass
    finally: cleanup()
