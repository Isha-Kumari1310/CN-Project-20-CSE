#!/usr/bin/env python3
"""
analyze.py - turns results/<scenario>_<mode>/ into tables and plots.

    python3 experiments/analyze.py [--results results]

Outputs (in <results>/):
    summary.csv / summary.md       one row per (scenario, mode)
    plots/<scenario>.png           latency over time + request share per server
    plots/summary_p95.png          p95 latency, static vs adaptive, all scenarios

Metrics
    throughput      successful requests per second
    mean/p50/p95/p99 latency (ms), measured by the client incl. retries
    error %         requests that failed even after all retries
    retried %       requests that needed more than one attempt
    jain_latency    Jain's fairness index over the per-server mean latency seen by
                    clients (1.0 = every server gives clients the same latency;
                    a slow/overloaded server that keeps getting its 'equal share'
                    pulls this down)
    detect (s)      failure injection -> controller marks the server down
    disrupt (s)     failure injection -> last client request that needed a retry
"""
import argparse
import csv
import glob
import json
import os
from collections import Counter, defaultdict


def pct(vals, p):
    if not vals:
        return float("nan")
    v = sorted(vals)
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def jain(xs):
    xs = [x for x in xs if x is not None]
    if not xs or sum(x * x for x in xs) == 0:
        return float("nan")
    return sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs))


def load_run(d):
    meta = json.load(open(os.path.join(d, "meta.json")))
    rows = []
    for f in glob.glob(os.path.join(d, "client_*.csv")):
        with open(f) as fh:
            for r in csv.DictReader(fh):
                rows.append({"t": float(r["t_start"]), "ok": int(r["ok"]),
                             "lat": float(r["latency_ms"]), "server": r["server"],
                             "attempts": int(r["attempts"])})
    events = []
    p = os.path.join(d, "lb_events.jsonl")
    if os.path.exists(p):
        for line in open(p):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return meta, sorted(rows, key=lambda r: r["t"]), events


def summarize(meta, rows, events):
    ok = [r for r in rows if r["ok"]]
    lats = [r["lat"] for r in ok]
    dur = meta["duration"]
    per_server = defaultdict(list)
    for r in ok:
        per_server[r["server"]].append(r["lat"])
    mean_lat = [sum(v) / len(v) for v in per_server.values() if v]
    detect = disrupt = None
    if meta["t_inject"]:
        for e in events:
            if e["ev"] == "server_down" and e["t"] >= meta["t_inject"]:
                detect = e["t"] - meta["t_inject"]
                break
        bad = [r["t"] + r["lat"] / 1000.0 for r in rows
               if r["t"] >= meta["t_inject"] - 0.5 and (r["attempts"] > 1 or not r["ok"])]
        disrupt = (max(bad) - meta["t_inject"]) if bad else 0.0
    return {
        "scenario": meta["scenario"], "mode": meta["mode"],
        "requests": len(rows),
        "throughput_rps": round(len(ok) / dur, 1),
        "mean_ms": round(sum(lats) / len(lats), 1) if lats else float("nan"),
        "p50_ms": round(pct(lats, 50), 1), "p95_ms": round(pct(lats, 95), 1),
        "p99_ms": round(pct(lats, 99), 1),
        "error_pct": round(100.0 * (len(rows) - len(ok)) / max(len(rows), 1), 2),
        "retried_pct": round(100.0 * sum(1 for r in rows if r["attempts"] > 1) / max(len(rows), 1), 2),
        "jain_latency": round(jain(mean_lat), 3),
        "detect_s": "" if detect is None else round(detect, 2),
        "disrupt_s": "" if disrupt is None else round(disrupt, 2),
    }


def plot(scenario, runs, outdir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.2),
                           gridspec_kw={"width_ratios": [2.2, 1]})
    colors = {"static": "#c0392b", "adaptive": "#1f77b4"}
    servers = sorted({r["server"] for _, rows, _ in runs.values() for r in rows if r["ok"]})
    width = 0.38
    for i, mode in enumerate(["static", "adaptive"]):
        if mode not in runs:
            continue
        meta, rows, _ = runs[mode]
        buckets = defaultdict(list)
        for r in rows:
            if r["ok"]:
                buckets[int(r["t"] - meta["t0"])].append(r["lat"])
        xs = sorted(buckets)
        ax[0].plot(xs, [sum(buckets[x]) / len(buckets[x]) for x in xs],
                   label=mode, color=colors[mode], lw=2)
        if meta["t_inject"]:
            ax[0].axvline(meta["t_inject"] - meta["t0"], color="gray", ls="--", lw=1)
        cnt = Counter(r["server"] for r in rows if r["ok"])
        tot = sum(cnt.values()) or 1
        ax[1].bar([j + (i - 0.5) * width for j in range(len(servers))],
                  [100.0 * cnt[s] / tot for s in servers], width,
                  label=mode, color=colors[mode])
    ax[0].set_title("%s: mean latency per second (dashed = event)" % scenario)
    ax[0].set_xlabel("time since clients started (s)")
    ax[0].set_ylabel("latency (ms)")
    ax[0].legend()
    ax[0].grid(alpha=0.3)
    ax[1].set_xticks(range(len(servers)))
    ax[1].set_xticklabels(servers)
    ax[1].set_ylabel("share of requests (%)")
    ax[1].set_title("requests per server")
    ax[1].legend()
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "%s.png" % scenario), dpi=140)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results"))
    args = ap.parse_args()
    plots = os.path.join(args.results, "plots")
    os.makedirs(plots, exist_ok=True)

    by_scenario = defaultdict(dict)
    summary = []
    for d in sorted(glob.glob(os.path.join(args.results, "*_*"))):
        if not os.path.exists(os.path.join(d, "meta.json")):
            continue
        meta, rows, events = load_run(d)
        by_scenario[meta["scenario"]][meta["mode"]] = (meta, rows, events)
        summary.append(summarize(meta, rows, events))
    if not summary:
        raise SystemExit("no results found in %s" % args.results)

    cols = list(summary[0].keys())
    with open(os.path.join(args.results, "summary.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(summary)
    with open(os.path.join(args.results, "summary.md"), "w") as fh:
        fh.write("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n")
        for r in summary:
            fh.write("| " + " | ".join(str(r[c]) for c in cols) + " |\n")
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print(open(os.path.join(args.results, "summary.md")).read())
        raise SystemExit("matplotlib missing -> tables written, plots skipped. "
                         "Fix: sudo apt install python3-matplotlib")
    for sc, runs in by_scenario.items():
        plot(sc, runs, plots)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    scs = list(by_scenario)
    fig, ax = plt.subplots(figsize=(7, 4))
    for i, mode in enumerate(["static", "adaptive"]):
        vals = [next((r["p95_ms"] for r in summary
                      if r["scenario"] == s and r["mode"] == mode), 0) for s in scs]
        ax.bar([j + (i - 0.5) * 0.38 for j in range(len(scs))], vals, 0.38,
               label=mode, color={"static": "#c0392b", "adaptive": "#1f77b4"}[mode])
    ax.set_xticks(range(len(scs)))
    ax.set_xticklabels(scs)
    ax.set_ylabel("p95 latency (ms)")
    ax.set_title("Static round-robin vs adaptive SDN load balancing")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(plots, "summary_p95.png"), dpi=140)
    print(open(os.path.join(args.results, "summary.md")).read())
    print("plots written to", plots)


if __name__ == "__main__":
    main()
