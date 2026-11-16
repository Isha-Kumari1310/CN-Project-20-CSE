# FINALUI — SDN Adaptive Load Balancer (Ubuntu)

This ZIP contains the complete **source** for a live Mininet/Open vSwitch lab, not just an HTML page. It combines the existing project with the Auto-mode controller and the faculty dashboard. It runs locally on compatible Ubuntu Linux with the necessary networking privileges; sending the ZIP through WhatsApp does not install the required software on someone else's laptop.

## Contents

- `ui/app.py` — start here: local web server and experiment controls.
- `ui/static/index.html` — interactive faculty dashboard.
- `controller/adaptive_lb.py` — OpenFlow 1.3 controller and Auto switching.
- `controller/lb_policy.py` — Round Robin / Adaptive scheduling.
- `controller/run_controller.py` — launcher for OS-Ken 4.x.
- `topology/lb_topo.py` — four clients, three TCP servers, Open vSwitch.
- `app/client.py`, `app/server.py` — actual TCP workload.
- `tests/` — policy tests and Auto switching unit tests.
- `experiments/` — experiment runner and analysis tools.

## How to run on your Ubuntu laptop

Open a terminal in the extracted `finalui` directory:

```bash
cd ~/Downloads/finalui   # change this path if you extracted elsewhere
python3 controller/run_controller.py --check
python3 -m unittest discover -s tests -p 'test_policy.py' -v
python3 -m unittest discover -s tests -p 'test_auto_switch.py' -v
sudo python3 ui/app.py
```

Open Firefox at **http://localhost:8080**. Click **Start network**, and then generate traffic. Use **Stop network** to shut down Mininet cleanly. Do not run another controller on port 6653 or another UI on port 8080 at the same time.

Do **not** run `setup.sh` on the original Ubuntu laptop if Mininet, Open vSwitch, Python and OS-Ken are already installed. The script is an older optional environment installer, not the normal launch path. The current OS-Ken launcher uses Python APIs instead of the removed `os_ken.cmd.manager` entry point.

If Start network fails, inspect the error notice and `/tmp/lb_ui_controller.log`. Check `sudo ss -ltnp | grep -E ':6653|:8080'`. If Mininet was left in a bad state, **stop the dashboard first**, then run `sudo mn -c`.

## Demonstration order

1. **Start network**: observe controller + switch + three TCP servers.
2. **Run 10 concurrent client loops**: observe real requests to the virtual IP, status, latency, and server assignment counts.
3. **Round Robin**: fixed rotation, independent of workload.
4. **Adaptive**: selects a healthy backend using estimated waiting time plus network utilization.
5. **Auto mode**: chooses Round Robin when calm; Adaptive if a server is stale or estimated utilization crosses threshold; waits for stable low load before returning.
6. **Full load (50)**: generates 50 concurrent client loops (not an automatic 100% CPU or utilization guarantee).
7. **Make Server 2 unresponsive**: SIGSTOP pauses it; heartbeat timeout should mark it unavailable. SIGCONT resumes it.
8. **Crash / recover / link down / link up**: separate application and network failure cases.
9. **Show OpenFlow rules**: controller-selected forwarding in Open vSwitch.
10. **Compare algorithms**: make measured comparisons using the same workload under both manual modes. Saved summary files are OPTIONAL and are not shipped with this source-only ZIP.

**Important limitations:** Auto switching affects *new* TCP connections; existing connections are not transparently migrated. Adaptive is not always faster than Round Robin. The UI's server utilization is an estimate, not CPU percentage. Compare real measurements before claiming any performance gain.

## Ubuntu requirements

On the original machine these were previously checked: Ubuntu 26.04, Python 3.14, Mininet, Open vSwitch, OS-Ken 4.1.1. An unfamiliar laptop must independently provide a supported Ubuntu/Mininet/OVS/OS-Ken environment and adequate privileges. The web server is bound to 127.0.0.1 for safety because its buttons can terminate processes and network links.

## Sharing on WhatsApp

Send **`finalui.zip` as a Document**. The recipient downloads and extracts it, then runs the commands above on a prepared Ubuntu system. It is *not* a standalone HTML website and will not run directly from WhatsApp or Windows without Linux networking support.

## Effective algorithm display fix (November 2026)
The dashboard now reads a small status file written by the SDN controller
every monitoring tick (`/tmp/lb_effective_mode.json`). Earlier versions
guessed the effective algorithm from the last 500 events; when a busy network
produced many request events, an older switch event disappeared and the UI
incorrectly displayed Round Robin. The new status file reports the actual
controller mode. To verify during a test:

```bash
sudo cat /tmp/lb_effective_mode.json
sudo cat /tmp/lb_control.json
```

If AUTO stays on Round Robin with sustained estimated utilization above 80%,
inspect `/tmp/lb_ui_controller.log` and the controller status file, rather
than lowering switching thresholds without evidence.
