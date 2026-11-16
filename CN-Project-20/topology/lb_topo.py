#!/usr/bin/env python3
"""
lb_topo.py - Mininet topology for the Adaptive Load Balancer.

            c1 c2 c3 c4          (clients, 10.0.0.1-4)
              \\ |  | /
               [ s1 ]  ---- Ryu controller (remote, 127.0.0.1:6653)
              /  |   \\
          srv1  srv2  srv3       (servers, 10.0.0.11-13, 10 Mbit/s links)

Virtual IP (VIP) 10.0.0.100 : clients connect here; the controller rewrites
packets to the chosen real server.
Heartbeat IP     10.0.0.254 : servers send UDP load reports here; the
controller reads them from packet-in.

Interactive use (controller must already be running):
    sudo python3 topology/lb_topo.py      (or: sudo .venv/bin/python topology/lb_topo.py)
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIP, VMAC, HB_IP = "10.0.0.100", "00:00:00:00:01:00", "10.0.0.254"
CLIENTS = [("c%d" % i, "10.0.0.%d" % i) for i in range(1, 5)]
# name, ip, relative speed, worker slots
SERVERS = [("srv1", "10.0.0.11", 1.0, 4),
           ("srv2", "10.0.0.12", 1.0, 4),
           ("srv3", "10.0.0.13", 0.5, 4)]     # srv3 is twice as slow


def build_net(controller_ip="127.0.0.1", controller_port=6653, link_mbps=10):
    from mininet.net import Mininet
    from mininet.node import RemoteController, OVSKernelSwitch
    from mininet.link import TCLink

    net = Mininet(controller=None, switch=OVSKernelSwitch, link=TCLink,
                  autoSetMacs=False, build=False)
    net.addController("c0", controller=RemoteController, ip=controller_ip,
                      port=controller_port)
    s1 = net.addSwitch("s1", protocols="OpenFlow13", failMode="secure")
    for i, (name, ip) in enumerate(CLIENTS, 1):
        h = net.addHost(name, ip=ip + "/24", mac="00:00:00:00:00:%02x" % i)
        net.addLink(h, s1, bw=100, delay="1ms")
    for i, (name, ip, _sp, _sl) in enumerate(SERVERS, 1):
        h = net.addHost(name, ip=ip + "/24", mac="00:00:00:00:00:%02x" % (0x10 + i))
        net.addLink(h, s1, bw=link_mbps, delay="1ms")
    net.build()
    net.start()
    try:
        net.waitConnected(timeout=15)
    except Exception:
        time.sleep(3)
    # permanent neighbour entries: hosts know each other, clients know the VIP,
    # servers know the heartbeat IP (the controller would also answer ARP).
    net.staticArp()
    for name, _ip in CLIENTS:
        _neigh(net.get(name), VIP)
    for name, *_ in SERVERS:
        _neigh(net.get(name), HB_IP)
    return net


def _neigh(host, ip):
    host.cmd("ip neigh replace %s lladdr %s dev %s nud permanent"
             % (ip, VMAC, host.defaultIntf().name))


def start_servers(net, logdir, hb_interval=0.5):
    procs = {}
    os.makedirs(logdir, exist_ok=True)
    for name, ip, speed, slots in SERVERS:
        fh = open(os.path.join(logdir, name + ".log"), "w")
        procs[name] = net.get(name).popen(
            ["python3", "-u", os.path.join(ROOT, "app", "server.py"),
             "--name", name, "--port", "8000", "--slots", str(slots),
             "--speed", str(speed), "--hb-ip", HB_IP, "--hb-interval",
             str(hb_interval)], stdout=fh, stderr=fh)
    return procs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=6653)
    ap.add_argument("--logdir", default="/tmp/lb_logs")
    args = ap.parse_args()
    from mininet.cli import CLI
    from mininet.log import setLogLevel
    setLogLevel("info")
    net = build_net(args.controller_ip, args.controller_port)
    procs = start_servers(net, args.logdir)
    cl = os.path.join(ROOT, "app", "client.py")
    print("\nServers started (give them ~2 s to register with the controller).\n"
          "Try, at the mininet> prompt:\n"
          "  c1 python3 %s --name c1 --duration 10 --out /tmp/c1.csv\n"
          "  sh ovs-ofctl -O OpenFlow13 dump-flows s1\n"
          "  srv2 kill -9 %s      (crash srv2)\n"
          "  srv1 kill -USR1 %s   (make srv1 4x slower; repeat to undo)\n"
          "  link srv2 s1 down    (link failure)\n"
          % (cl, procs["srv2"].pid, procs["srv1"].pid))
    CLI(net)
    for p in procs.values():
        p.terminate()
    net.stop()


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("Run with sudo (Mininet needs root).")
    main()
