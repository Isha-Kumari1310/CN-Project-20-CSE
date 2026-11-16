
# Adaptive Load Balancer (SDN + TCP sockets) - Project 20

> \*\*Problem statement (Project 20):\*\* create multiple TCP application servers and
> dynamically distribute client traffic based on server and network load. Detect
> overloaded servers and failed servers, and compare static and adaptive load balancing.

Protocol: **TCP** · Network: **Mininet + Open vSwitch (OpenFlow 1.3)** · Controller: **os-ken / Ryu**

**TEAM-MATES->**

**----------------------------------------------------------------------------------------------------------------------------------------------**

**NAME: Isha Kumari**

**SRN:PES1UG25CS213**

**CONTRIBUTION: IMPLEMENTATION AND INTEGRATION OF TCP SERVER-CLIENT**

**----------------------------------------------------------------------------------------------------------------------------------------------**

**NAME:KAILAS NATH A** 

**SRN:PES1UG25CS238**

**CONTRIBUTION: Mininet Topology Setup and SDN Controller integration**



**----------------------------------------------------------------------------------------------------------------------------------------------**

**NAME:Skanda**

**SRN:PES1UG25CS233**

**CONTRIBUTION: UI and  Testing**



**----------------------------------------------------------------------------------------------------------------------------------------------**





\---

## 1\. Architecture

```
   c1   c2   c3   c4                      Ryu controller (adaptive\_lb.py)
    \\    |    |    /                        - ARP proxy for VIP
     \\   |    |   /   OpenFlow 1.3          - reads server heartbeats (packet-in)
      +--\[  s1  ]------------------------   - polls port statistics (net load)
          / | \\                             - picks server (lb\_policy.py)
         /  |  \\                            - installs rewrite flows, removes
     srv1 srv2 srv3  (10 Mbit/s links)        flows of dead servers
     (srv3 is 2x slower)

 clients connect to the VIP 10.0.0.100:8000  -> controller rewrites to a real server
 servers send UDP heartbeats to 10.0.0.254:9999 -> controller reads them
```

**Data path of one request**

1. Client opens TCP to `10.0.0.100:8000`. The SYN has no flow -> **packet-in**.
2. Controller picks a server (adaptive score or round-robin) and installs two flows:
`client->VIP` is rewritten to `client->server`, `server->client` is rewritten back so it
looks like it came from the VIP. The switch now forwards the whole connection alone.
3. Server does the work and answers. Client records latency and which server answered.

**Two kinds of load information reach the controller**

|Load type|How it is obtained|Where|
|-|-|-|
|Server (application) load|UDP heartbeat JSON from `server.py`: active requests, slots, service time|socket -> packet-in -> `on\_heartbeat()`|
|Network load|OpenFlow **port statistics** of each server port every 0.5 s|`on\_port\_stats()` -> `update\_net()`|

**Adaptive policy** (`controller/lb\_policy.py`)

```
expected\_wait = (est\_active + 1) \* svc\_ms / slots
score         = 0.7 \* expected\_wait / 100ms  +  0.3 \* link\_utilisation
pick          = live, non-overloaded server with the lowest score
overloaded    = est\_active / slots > 1.25
dead          = no heartbeat for 2 s   OR   switch port is down
```

`est\_active` = connections the server reported + flows the controller assigned since that report
(prevents sending a whole burst to the same server between two heartbeats).

**Static baseline** = plain round-robin over all registered servers; ignores load, speed and health.

\---

## 2\. Files

```
controller/lb\_policy.py      decision logic (pure Python, unit-tested)
controller/adaptive\_lb.py    OpenFlow 1.3 application (os-ken or Ryu, auto-detected)
controller/run\_controller.py launcher: picks the working framework, no PATH problems
app/server.py                TCP server (threads, worker slots, heartbeat, SIGUSR1 = degrade)
app/client.py                TCP client / load generator with retries, writes CSV
topology/lb\_topo.py          Mininet topology (4 clients, 3 servers, 1 switch) + interactive mode
experiments/run\_experiments.py   runs 4 scenarios x {static, adaptive}
experiments/analyze.py       summary table + plots
tests/test\_policy.py         14 unit tests of the policy
tests/test\_controller\_offline.py  8 tests of the controller handlers (fake switch, real Ryu packet/OpenFlow classes)
tests/local\_sim.py           Mininet-free smoke test (user-space proxy + same policy)
setup.sh / run\_all.sh        install dependencies / run everything
```

\---

## 3\. Setup (Ubuntu 20.04 / 22.04 / 24.04 VM, as in the Mininet manual)

```bash
unzip sdn\_adaptive\_lb.zip \&\& cd sdn\_adaptive\_lb
chmod +x setup.sh run\_all.sh
./setup.sh
```

`setup.sh` installs Mininet + Open vSwitch + matplotlib with apt, then creates a virtualenv `.venv`
(with `--system-site-packages`, so Mininet is visible) and installs **os-ken** in it.
os-ken is the maintained, API-compatible fork of Ryu; the original Ryu 4.34 crashes on Python >= 3.10.
Nothing is installed with `sudo pip`, so Ubuntu 24.04's *externally-managed-environment* error cannot occur.

Check:

```bash
sudo mn --test pingall                          # 0% dropped
.venv/bin/python controller/run\_controller.py --check   # OK: using os\_ken ...
```

> Always start Python with `.venv/bin/python` (also under sudo: `sudo .venv/bin/python ...`).
> Plain `python3` does not see os-ken.

\---

## 4\. Run

### 4a. Everything automatically (about 8 minutes)

```bash
./run\_all.sh
```

= unit tests, then `sudo .venv/bin/python experiments/run\_experiments.py`, then `analyze.py`.
Results: `results/summary.md`, `results/summary.csv`, `results/plots/\*.png`, raw data in `results/<scenario>\_<mode>/`.

One scenario only (quick check, \~1 min): `./run\_all.sh --scenarios crash --duration 20`

### 4b. Manual demo (good for the viva) - needs 2 terminals, both in the project folder

Terminal 1 - controller:

```bash
.venv/bin/python controller/run\_controller.py --mode adaptive     # or: --mode static
```

Terminal 2 - network + servers:

```bash
sudo mn -c
sudo .venv/bin/python topology/lb\_topo.py
```

Then at the `mininet>` prompt (the topology script prints these with the right PIDs):

```
mininet> c1 python3 app/client.py --name c1 --duration 10 --out /tmp/c1.csv
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1        # see the rewrite flows
mininet> srv2 kill -9 <pid printed at start>              # crash a server
mininet> link srv2 s1 down                                # or: link failure
mininet> c1 python3 app/client.py --name c1 --duration 10 --out /tmp/c1b.csv
```

Controller event log: `/tmp/lb\_events.jsonl` (`server\_up`, `assign`, `server\_down`, ...).
Stop the controller with Ctrl-C, restart it with `--mode static` to show the baseline.

### 4c. No-Mininet checks (work on any machine, no root)

```bash
python3 -m unittest -v tests/test\_policy.py                     # 14 policy tests
.venv/bin/python -m unittest -v tests/test\_controller\_offline.py # 8 controller tests (needs os-ken)
python3 tests/local\_sim.py \&\& python3 experiments/analyze.py --results results\_local\_sim
```

\---

## 5\. Scenarios and metrics

|Scenario|Event at t = 10 s|What it tests|
|-|-|-|
|normal|none (srv3 is 2x slower from the start)|load/capacity-aware distribution|
|overload|srv1 becomes 4x slower (SIGUSR1)|detect an overloaded server, steer away|
|crash|srv2 process is killed|failure detection by heartbeat timeout|
|linkfail|srv2-s1 link goes down|failure detection by OpenFlow port-status|

Metrics: throughput, mean / p50 / p95 / p99 latency, error %, retried %, Jain fairness of
per-server latency, failure **detect time** (event -> controller marks server down) and
client-visible **disruption** (event -> last request that needed a retry).

\---

## 6\. Controller settings (environment variables)

|Variable|Default|Meaning|
|-|-|-|
|`LB\_MODE`|adaptive|`adaptive` or `static`|
|`LB\_W\_TIME`, `LB\_W\_NET`|0.7, 0.3|weights of server-time vs network term|
|`LB\_OVERLOAD`|1.25|overload threshold (est\_active / slots)|
|`LB\_HB\_TIMEOUT`|2.0|seconds without heartbeat -> server dead|
|`LB\_FLOW\_IDLE`|5|idle timeout of installed flows (s)|
|`LB\_LINK\_BW\_MBPS`|10|server link capacity used for network utilisation|
|`LB\_LOG`|/tmp/lb\_events.jsonl|event log|

\---

## 7\. Troubleshooting

|Symptom|Fix|
|-|-|
|`No working os-ken / ryu found`|`./setup.sh`; and run with `.venv/bin/python`, not plain `python3`|
|`ImportError: cannot import name 'ALREADY\_HANDLED' from 'eventlet.wsgi'`|old Ryu + new eventlet. Use os-ken (`./setup.sh` does it)|
|`error: externally-managed-environment`|never `sudo pip install`; `setup.sh` uses a venv|
|`ModuleNotFoundError: No module named 'mininet'`|`sudo apt install mininet`; venv must be created with `--system-site-packages` (delete `.venv`, rerun `./setup.sh`)|
|`Something already listens on port 6653`|`sudo fuser -k 6653/tcp` (old controller), then `sudo mn -c`|
|`ovs-vsctl: database connection failed`|`sudo service openvswitch-switch start`|
|`PermissionError ... results/plots`|old problem (results owned by root); fixed - or `sudo chown -R $USER results`|
|Servers never show `server\_up`|check `sudo ovs-vsctl show` -> `is\_connected: true`; read `results/<run>/lb\_events.jsonl.ryu.out`|
|Leftovers after a crashed run|`sudo mn -c`|
|Results too light/heavy for your VM|change `--threads`, `--think-ms`, `--work-ms` of `run\_experiments.py`|

## 8\. Known limitations (be honest about these in the viva)

* One switch (the LB decision is made at the edge); a multi-switch topology would need path selection too.
* Flows are per-connection; a connection already established on a server that then dies is lost
and the *client* retries (the SDN controller cannot migrate a TCP connection).
* Heartbeat detection takes up to `LB\_HB\_TIMEOUT` (2 s) + one monitor tick; link failures are detected
immediately via port-status.
* The score weights and thresholds are hand-tuned for this topology, not learned.

