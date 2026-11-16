#!/usr/bin/env bash
# Runs unit tests, every scenario (static + adaptive), then builds tables + plots.
#   ./run_all.sh                       # full run (~8 min)
#   ./run_all.sh --scenarios crash --duration 20
set -e
cd "$(dirname "$0")"
PY="$(pwd)/.venv/bin/python"
[ -x "$PY" ] || { echo "Run ./setup.sh first (creates .venv)"; exit 1; }

$PY -m unittest -v tests/test_policy.py
sudo "$PY" experiments/run_experiments.py "$@"
"$PY" experiments/analyze.py
echo "See results/summary.md and results/plots/"
