#!/usr/bin/env python3
"""
run_experiments.py - runs every scenario for BOTH policies and stores raw data.

    sudo python3 experiments/run_experiments.py                # everything
    sudo python3 experiments/run_experiments.py --scenarios normal crash
    sudo python3 experiments/run_experiments.py --duration 20

Scenarios (the failure/overload event is injected at --inject seconds):
    normal    no event; heterogeneous servers (srv3 is 2x slower)
    overload  srv1 becomes 4x slower (SIGUSR1 -> degraded mode)
    crash     srv2's server process is killed (SIGKILL)
    linkfail  the srv2 <-> s1 link is taken down

For each (scenario, mode) the script
    1. starts the controller (os-ken/Ryu) with LB_MODE=<mode>,
    2. builds the Mininet topology and starts 3 servers,
    3. starts 4 clients (each with several concurrent request loops),
    4. injects the event, waits for the clients, and
    5. saves client CSVs + the controller's event log in results/<scenario>_<mode>/
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "topology"))

import lb_topo  # noqa: E402

SCENARIOS = ["normal", "overload", "crash", "linkfail"]
MODES = ["static", "adaptive"]


def port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start_controller(mode, log_path, port=6653):
    """Start the controller with THIS python (works with venv + sudo)."""
    cmd = [sys.executable, os.path.join(ROOT, "controller", "run_controller.py"),
           "--mode", mode, "--log", log_path, "--port", str(port)]
    out = open(log_path + ".ryu.out", "w")
    p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
    for _ in range(60):                       # wait up to 15 s until it listens
        if p.poll() is not None:
            sys.exit("controller exited early - see %s.ryu.out\n%s"
                     % (log_path, open(log_path + ".ryu.out").read()[-1500:]))
        if port_open(port):
            return p
        time.sleep(0.25)
    p.kill()
    sys.exit("controller did not start listening on port %d - see %s.ryu.out"
             % (port, log_path))


def count_events(log_path, kind):
    try:
        return sum(1 for line in open(log_path) if '"ev": "%s"' % kind in line)
    except OSError:
        return 0


def wait_servers_registered(log_path, n, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if count_events(log_path, "server_up") >= n:
            return True
        time.sleep(0.25)
    return False


def preflight():
    """Fail early, with a readable message, instead of a Python traceback."""
    if os.geteuid() != 0:
        sys.exit("Run with sudo (Mininet needs root):  sudo %s %s"
                 % (sys.executable, " ".join(sys.argv)))
    try:
        import mininet  # noqa: F401
    except ImportError:
        sys.exit("Python module 'mininet' not found for %s.\n"
                 "Install it (sudo apt install mininet) and create the venv with "
                 "--system-site-packages (./setup.sh does this)." % sys.executable)
    if subprocess.call(["ovs-vsctl", "--timeout=5", "show"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        print("Open vSwitch not running - trying to start it ...")
        subprocess.call(["service", "openvswitch-switch", "start"])
        time.sleep(2)
        if subprocess.call(["ovs-vsctl", "--timeout=5", "show"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
            sys.exit("Open vSwitch is not working: sudo apt install openvswitch-switch && "
                     "sudo service openvswitch-switch start")
    rc = subprocess.call([sys.executable, os.path.join(ROOT, "controller",
                                                       "run_controller.py"), "--check"])
    if rc != 0:
        sys.exit("No working os-ken/ryu for %s. Run ./setup.sh first." % sys.executable)
    if port_open(6653):
        sys.exit("Something already listens on port 6653 (an old controller?).\n"
                 "Stop it:  sudo fuser -k 6653/tcp   then run again.")


def fix_owner(path):
    """results/ is created by root (sudo); give it back to the real user so that
    analyze.py can write plots without sudo."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid is None:
        return
    for dp, dns, fns in os.walk(path):
        for n in [dp] + [os.path.join(dp, x) for x in dns + fns]:
            try:
                os.chown(n, int(uid), int(gid))
            except OSError:
                pass


def run_one(scenario, mode, args):
    outdir = os.path.join(args.results, "%s_%s" % (scenario, mode))
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    ev_log = os.path.join(outdir, "lb_events.jsonl")
    print("\n=== %s / %s ===" % (scenario, mode), flush=True)
    ryu = start_controller(mode, ev_log)
    net = None
    servers = {}
    try:
        net = lb_topo.build_net()
        servers = lb_topo.start_servers(net, outdir)
        if not wait_servers_registered(ev_log, len(lb_topo.SERVERS)):
            print("  WARNING: not all servers registered with the controller "
                  "(check %s and %s.ryu.out)" % (os.path.join(outdir, "srv1.log"), ev_log))
        time.sleep(1.0)

        clients = []
        t0 = time.time()
        for name, _ip in lb_topo.CLIENTS:
            h = net.get(name)
            out = os.path.join(outdir, "client_%s.csv" % name)
            log = open(os.path.join(outdir, "client_%s.log" % name), "w")
            clients.append(h.popen(
                ["python3", "-u", os.path.join(ROOT, "app", "client.py"),
                 "--name", name, "--duration", str(args.duration),
                 "--threads", str(args.threads), "--work-ms", str(args.work_ms),
                 "--resp-bytes", str(args.resp_bytes),
                 "--think-ms", str(args.think_ms), "--out", out],
                stdout=log, stderr=log))

        t_inject = None
        if scenario != "normal":
            time.sleep(args.inject)
            t_inject = time.time()
            if scenario == "overload":
                os.kill(servers["srv1"].pid, signal.SIGUSR1)
            elif scenario == "crash":
                servers["srv2"].kill()
            elif scenario == "linkfail":
                net.configLinkStatus("srv2", "s1", "down")
            print("  event '%s' injected at +%.1fs" % (scenario, t_inject - t0))

        for c, (name, _ip) in zip(clients, lb_topo.CLIENTS):
            rc = c.wait()
            if rc != 0:
                logf = os.path.join(outdir, "client_%s.log" % name)
                print("  WARNING: client %s exited with code %s:\n%s"
                      % (name, rc, open(logf).read()[-800:]))
        with open(os.path.join(outdir, "meta.json"), "w") as fh:
            json.dump({"scenario": scenario, "mode": mode, "t0": t0,
                       "t_inject": t_inject, "duration": args.duration}, fh)
    finally:
        for p in servers.values():
            try:
                p.kill()
            except Exception:
                pass
        if net is not None:
            net.stop()
        ryu.terminate()
        try:
            ryu.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ryu.kill()
        subprocess.call(["mn", "-c"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", nargs="+", default=SCENARIOS, choices=SCENARIOS)
    ap.add_argument("--modes", nargs="+", default=MODES, choices=MODES)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--inject", type=float, default=10.0)
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--work-ms", type=int, default=100)
    ap.add_argument("--resp-bytes", type=int, default=20000)
    ap.add_argument("--think-ms", type=float, default=20.0)
    ap.add_argument("--results", default=os.path.join(ROOT, "results"))
    args = ap.parse_args()
    preflight()
    subprocess.call(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.makedirs(args.results, exist_ok=True)
    try:
        for sc in args.scenarios:
            for mode in args.modes:
                run_one(sc, mode, args)
    finally:
        fix_owner(args.results)
    print("\nDone. Now run:  python3 experiments/analyze.py")


if __name__ == "__main__":
    main()
