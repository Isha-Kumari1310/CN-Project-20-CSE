#!/usr/bin/env python3
"""
client.py - TCP load generator / client for the Adaptive Load Balancer project.

Each request opens a NEW TCP connection to the virtual IP (VIP). A new
connection = a new flow = a new decision by the SDN controller.

If a request fails (reset / timeout, e.g. the chosen server just died) the
client retries on a fresh connection (a new source port, so the controller
makes a new choice). Latency is measured from the first attempt, so retries
show up as extra latency - that is the client-visible cost of a failure.

Output CSV columns:
    t_start (epoch s), client, req_id, ok, latency_ms, server, attempts
"""
import argparse
import csv
import random
import os
import socket
import threading
import time


def one_attempt(vip, port, rid, work_ms, nbytes, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((vip, port))
        s.sendall(("REQ %s %d %d\n" % (rid, work_ms, nbytes)).encode())
        f = s.makefile("rb")
        header = f.readline().split()
        if len(header) != 4 or header[0] != b"OK":
            return None
        server = header[2].decode()
        remaining = int(header[3])
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                return None
            remaining -= len(chunk)
        return server
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def worker(idx, args, rows, lock, deadline):
    n = 0
    while time.time() < deadline:
        n += 1
        rid = "%s-%d-%d" % (args.name, idx, n)
        t0 = time.time()
        server, attempts = None, 0
        while attempts <= args.retries and server is None:
            attempts += 1
            server = one_attempt(args.vip, args.port, rid, args.work_ms,
                                 args.resp_bytes, args.timeout)
            if server is None:
                time.sleep(0.05)
        lat = (time.time() - t0) * 1000.0
        with lock:
            rows.append((round(t0, 4), args.name, rid, int(server is not None),
                         round(lat, 2), server or "-", attempts))
        if args.think_ms > 0:
            time.sleep(random.uniform(0.5, 1.5) * args.think_ms / 1000.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", required=True)
    ap.add_argument("--vip", default="10.0.0.100")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--threads", type=int, default=3,
                    help="concurrent request loops")
    ap.add_argument("--work-ms", type=int, default=100)
    ap.add_argument("--job-mix", choices=["uniform", "mixed"], default="uniform",
                    help="mixed creates 25, 50, 100, 250, 500 ms jobs for SJF demos")
    ap.add_argument("--resp-bytes", type=int, default=20000)
    ap.add_argument("--think-ms", type=float, default=20.0)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows, lock = [], threading.Lock()
    deadline = time.time() + args.duration
    # Keep the existing CSV schema but write each completed request immediately.
    # The UI can tail this file while the workload is still running.
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="", buffering=1) as fh:
        w = csv.writer(fh)
        w.writerow(["t_start", "client", "req_id", "ok", "latency_ms",
                    "server", "attempts"])
        fh.flush()

        def live_worker(idx):
            n = 0
            while time.time() < deadline:
                n += 1
                rid = "%s-%d-%d" % (args.name, idx, n)
                t0 = time.time()
                work_ms = random.choice((25, 50, 100, 250, 500)) if args.job_mix == "mixed" else args.work_ms
                server, attempts = None, 0
                while attempts <= args.retries and server is None:
                    attempts += 1
                    server = one_attempt(args.vip, args.port, rid, work_ms,
                                         args.resp_bytes, args.timeout)
                    if server is None:
                        time.sleep(0.05)
                row = (round(t0, 4), args.name, rid, int(server is not None),
                       round((time.time() - t0) * 1000.0, 2), server or "-", attempts)
                with lock:
                    rows.append(row)
                    w.writerow(row)
                    fh.flush()
                if args.think_ms > 0:
                    time.sleep(random.uniform(0.5, 1.5) * args.think_ms / 1000.0)

        ts = [threading.Thread(target=live_worker, args=(i,), daemon=True)
              for i in range(args.threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    ok = sum(r[3] for r in rows)
    print("[%s] %d requests, %d ok -> %s" % (args.name, len(rows), ok, args.out),
          flush=True)


if __name__ == "__main__":
    main()
