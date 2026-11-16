#!/usr/bin/env python3
"""
local_sim.py - Mininet-free smoke test of the application + policy.

A user-space TCP proxy plays the part of "SDN switch + controller": it listens
on 127.0.0.1:9000 (the 'VIP'), receives the servers' UDP heartbeats on :9999
and chooses a backend with the SAME lb_policy.LoadBalancer used by the Ryu
controller. Real servers (app/server.py) and real clients (app/client.py) run
as separate processes.

This verifies the sockets, the heartbeat format, the policy and the analysis
scripts. It is NOT a replacement for the Mininet + Ryu experiments (there the
forwarding really happens in OpenFlow switches).

    python3 tests/local_sim.py [--duration 16] [--scenarios normal overload crash]
Output goes to results_local_sim/ ; analyse it with
    python3 experiments/analyze.py --results results_local_sim
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "controller"))
from lb_policy import LoadBalancer  # noqa: E402

PROXY_PORT, HB_PORT = 9000, 9999
BACKENDS = [("srv1", 9001, 1.0), ("srv2", 9002, 1.0), ("srv3", 9003, 0.5)]


class Proxy(object):
    def __init__(self, mode, ev_path):
        self.lb = LoadBalancer(mode=mode, link_bps=1e12)
        self.lock = threading.Lock()
        self.ev = open(ev_path, "w")
        self.stop = False
        self.ports = {}

    def log(self, **kw):
        kw["t"] = time.time()
        self.ev.write(json.dumps(kw) + "\n")
        self.ev.flush()

    def hb_loop(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", HB_PORT))
        s.settimeout(0.2)
        while not self.stop:
            try:
                data, _ = s.recvfrom(4096)
                hb = json.loads(data.decode())
            except (socket.timeout, ValueError):
                continue
            with self.lock:
                self.ports[hb["name"]] = hb["port"]
                srv, up = self.lb.on_heartbeat(hb, "127.0.0.1", "-", hb["port"])
            if up:
                self.log(ev="server_up", server=srv.name)

    def monitor_loop(self):
        tick = 0
        while not self.stop:
            time.sleep(0.5)
            with self.lock:
                for srv in self.lb.expire():
                    if self.lb.mode == "adaptive":
                        self.log(ev="server_down", server=srv.name, reason="heartbeat_timeout")
                tick += 1
                if tick % 2 == 0:
                    self.log(ev="state", servers=self.lb.snapshot())

    def serve(self):
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("127.0.0.1", PROXY_PORT))
        ls.listen(256)
        ls.settimeout(0.3)
        while not self.stop:
            try:
                c, _ = ls.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self.relay, args=(c,), daemon=True).start()
        ls.close()

    def relay(self, c):
        with self.lock:
            srv = self.lb.pick()
        if srv is None:
            c.close()
            return
        try:
            b = socket.create_connection(("127.0.0.1", srv.tcp_port), timeout=1.0)
        except OSError:
            c.close()
            return

        def pipe(a, z):
            try:
                while True:
                    d = a.recv(65536)
                    if not d:
                        break
                    z.sendall(d)
            except OSError:
                pass
            finally:
                for x in (a, z):
                    try:
                        x.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        threading.Thread(target=pipe, args=(c, b), daemon=True).start()
        pipe(b, c)
        c.close()
        b.close()


def run(scenario, mode, args):
    out = os.path.join(args.results, "%s_%s" % (scenario, mode))
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    print("=== %s / %s ===" % (scenario, mode), flush=True)
    px = Proxy(mode, os.path.join(out, "lb_events.jsonl"))
    for fn in (px.hb_loop, px.monitor_loop, px.serve):
        threading.Thread(target=fn, daemon=True).start()
    servers = {}
    for name, port, speed in BACKENDS:
        servers[name] = subprocess.Popen(
            [sys.executable, "-u", os.path.join(ROOT, "app", "server.py"),
             "--name", name, "--bind", "127.0.0.1", "--port", str(port),
             "--speed", str(speed), "--hb-ip", "127.0.0.1", "--hb-port", str(HB_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.0)
    t0 = time.time()
    clients = [subprocess.Popen(
        [sys.executable, "-u", os.path.join(ROOT, "app", "client.py"),
         "--name", "c%d" % i, "--vip", "127.0.0.1", "--port", str(PROXY_PORT),
         "--duration", str(args.duration), "--threads", "3",
         "--out", os.path.join(out, "client_c%d.csv" % i)],
        stdout=subprocess.DEVNULL) for i in range(1, 5)]
    t_inject = None
    if scenario != "normal":
        time.sleep(args.inject)
        t_inject = time.time()
        if scenario == "overload":
            servers["srv1"].send_signal(signal.SIGUSR1)
        else:
            servers["srv2"].kill()
    for c in clients:
        c.wait()
    json.dump({"scenario": scenario, "mode": mode, "t0": t0, "t_inject": t_inject,
               "duration": args.duration}, open(os.path.join(out, "meta.json"), "w"))
    px.stop = True
    for p in servers.values():
        p.kill()
    time.sleep(0.6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=16.0)
    ap.add_argument("--inject", type=float, default=6.0)
    ap.add_argument("--scenarios", nargs="+", default=["normal", "overload", "crash"])
    ap.add_argument("--results", default=os.path.join(ROOT, "results_local_sim"))
    args = ap.parse_args()
    os.makedirs(args.results, exist_ok=True)
    for sc in args.scenarios:
        for mode in ("static", "adaptive"):
            run(sc, mode, args)


if __name__ == "__main__":
    main()
