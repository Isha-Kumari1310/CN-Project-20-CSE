#!/usr/bin/env python3
"""Run the existing OpenFlow app using OS-Ken 4.x Python APIs."""
import argparse
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "adaptive_lb.py")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["static", "adaptive", "sjf", "auto"],
                        default=os.environ.get("LB_MODE", "adaptive"))
    parser.add_argument("--port", type=int, default=6653)
    parser.add_argument("--log", default=os.environ.get("LB_LOG", "/tmp/lb_events.jsonl"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for mod in ("os_ken.base.app_manager", "os_ken.controller.ofp_event", "os_ken.ofproto.ofproto_v1_3"):
        importlib.import_module(mod)
    if args.check:
        print("OS-Ken APIs READY using", sys.executable)
        return
    os.environ["LB_BACKEND"] = "os_ken"
    os.environ["LB_MODE"] = args.mode
    os.environ["LB_LOG"] = args.log
    from os_ken import cfg
    from os_ken.base import app_manager
    cfg.CONF(args=["--ofp-tcp-listen-port", str(args.port)], project="adaptive-load-balancer")
    print("Controller starting: requested mode=%s, port=%d" % (args.mode, args.port), flush=True)
    app_manager.AppManager.run_apps([APP])

if __name__ == "__main__":
    main()
