#!/usr/bin/env python3
"""
server.py - TCP application server for the Adaptive Load Balancer project.

* Plain sockets (socket.socket), one thread per connection.
* Protocol (one request per connection, line based):
      client -> server :  REQ <id> <work_ms> <resp_bytes>\n
      server -> client :  OK <id> <server_name> <resp_bytes>\n  + payload bytes
* The server has `--slots` worker threads. Requests beyond that queue.
  In SJF mode workers pick the shortest estimated work_ms from their queue;
  in RR/Adaptive mode each worker picks the earliest arrival (FIFO).
* `--speed` < 1 makes the server slower (heterogeneous servers).
* SIGUSR1 toggles "degraded" mode (service time x --degrade) to simulate a
  noisy neighbour / overloaded machine in the middle of an experiment.
* A UDP heartbeat is sent every --hb-interval seconds to --hb-ip:--hb-port.
  In Mininet that address is a *virtual* IP that the SDN controller answers
  for; the controller reads the heartbeat from the packet-in. This is how
  the application socket layer feeds the SDN control plane.
"""
import argparse
import collections
import os
import json
import signal
import socket
import threading
import time


class State(object):
    def __init__(self, slots):
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.slots = slots
        self.pending = []                 # (sequence, connection, request)
        self.sequence = 0
        self.active = 0                  # executing + queued (same as before)
        self.served = 0
        self.svc_ms = None
        self.degraded = False
        self.work_samples = collections.deque(maxlen=64)
        self.sjf_dispatched = 0
        self.fifo_dispatched = 0
        self.last_job_ms = 0.0


def current_mode():
    """Mininet processes share a filesystem. The OS-Ken controller publishes
    its authoritative mode here every 0.5s. If missing/stale, fall back FIFO.
    The mode determines the ordering of jobs WAITING for a worker; it cannot
    preempt jobs already executing.
    """
    path = os.environ.get("LB_STATUS", "/tmp/lb_effective_mode.json")
    try:
        with open(path) as f:
            data = json.load(f)
        if time.time() - float(data.get("t", 0)) < 3:
            return data.get("effective_mode", "static")
    except (OSError, ValueError, TypeError):
        pass
    return "static"


def handle(conn, st, args):
    """Read a request and enqueue it; worker threads do actual processing.
    SJF uses the requested work_ms from REQ, which was NOT available during
    the TCP SYN handled by OpenFlow. This is real server-side job scheduling.
    """
    try:
        conn.settimeout(10)
        line = conn.makefile("rb").readline()
        parts = line.split()
        if len(parts) != 4 or parts[0] != b"REQ":
            conn.sendall(b"ERR bad-request\n")
            conn.close()
            return
        rid = parts[1].decode()
        work_ms = float(parts[2])
        nbytes = int(parts[3])
        if not 0 <= work_ms <= 10000 or not 0 <= nbytes <= 1000000:
            raise ValueError("Invalid request size")
        with st.condition:
            st.sequence += 1
            st.active += 1
            st.pending.append((st.sequence, conn, rid, work_ms, nbytes, time.time()))
            st.work_samples.append(work_ms)
            st.condition.notify()
    except (OSError, ValueError, UnicodeError):
        try: conn.close()
        except OSError: pass


def pending_index(pending, mode):
    """Choose next queued request: shortest work first in SJF, FIFO otherwise."""
    if mode == "sjf":
        return min(range(len(pending)), key=lambda i: (pending[i][3], pending[i][0]))
    return min(range(len(pending)), key=lambda i: pending[i][0])


def worker_loop(st, args):
    while True:
        # Read current algorithm before taking the next waiting job.
        mode = current_mode()
        with st.condition:
            while not st.pending:
                st.condition.wait()
            i = pending_index(st.pending, mode)
            _seq, conn, rid, work_ms, nbytes, queued_at = st.pending.pop(i)
            if mode == "sjf":
                st.sjf_dispatched += 1
            else:
                st.fifo_dispatched += 1
            st.last_job_ms = work_ms
        try:
            factor = 1.0 / args.speed
            if st.degraded:
                factor *= args.degrade
            t0 = time.time()
            time.sleep(work_ms * factor / 1000.0)
            svc = (time.time() - t0) * 1000.0
            with st.lock:
                st.svc_ms = svc if st.svc_ms is None else 0.7 * st.svc_ms + 0.3 * svc
            conn.sendall(("OK %s %s %d\n" % (rid, args.name, nbytes)).encode()
                         + b"x" * nbytes)
        except OSError:
            pass
        finally:
            with st.lock:
                st.active -= 1
                st.served += 1
            try: conn.close()
            except OSError: pass


def heartbeat_loop(st, args):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    nominal = args.nominal_ms / args.speed
    while True:
        with st.lock:
            samples = list(st.work_samples)
            jmin = min(samples) if samples else 0.0
            jmax = max(samples) if samples else 0.0
            job_mix = len(samples) >= 8 and jmin > 0 and jmax / jmin >= 3.0
            msg = {
                "name": args.name,
                "port": args.port,
                "slots": args.slots,
                "active": st.active,
                "served": st.served,
                "svc_ms": round(st.svc_ms if st.svc_ms else nominal, 1),
                "degraded": st.degraded,
                "job_mix": job_mix,
                "job_samples": len(samples),
                "job_min_ms": jmin,
                "job_max_ms": jmax,
                "pending_jobs": len(st.pending),
                "sjf_dispatched": st.sjf_dispatched,
                "fifo_dispatched": st.fifo_dispatched,
                "last_job_ms": st.last_job_ms,
                "ts": time.time(),
            }
        try:
            sock.sendto(json.dumps(msg).encode(), (args.hb_ip, args.hb_port))
        except OSError:
            pass                  # network not ready yet; try again
        time.sleep(args.hb_interval)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--slots", type=int, default=4, help="concurrent workers")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="relative speed (0.5 = twice as slow)")
    ap.add_argument("--degrade", type=float, default=4.0,
                    help="slowdown factor when SIGUSR1 toggles degraded mode")
    ap.add_argument("--nominal-ms", type=float, default=100.0,
                    help="expected service time before any request is seen")
    ap.add_argument("--hb-ip", default="10.0.0.254")
    ap.add_argument("--hb-port", type=int, default=9999)
    ap.add_argument("--hb-interval", type=float, default=0.5)
    args = ap.parse_args()

    st = State(args.slots)

    def toggle(_sig, _frm):
        st.degraded = not st.degraded
        print("[%s] degraded=%s" % (args.name, st.degraded), flush=True)

    signal.signal(signal.SIGUSR1, toggle)

    for _ in range(args.slots):
        threading.Thread(target=worker_loop, args=(st, args), daemon=True).start()
    threading.Thread(target=heartbeat_loop, args=(st, args), daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(256)
    print("[%s] listening on %s:%d slots=%d speed=%.2f" %
          (args.name, args.bind, args.port, args.slots, args.speed), flush=True)
    while True:
        conn, _addr = srv.accept()
        threading.Thread(target=handle, args=(conn, st, args), daemon=True).start()


if __name__ == "__main__":
    main()
