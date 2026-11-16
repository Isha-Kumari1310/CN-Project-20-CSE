# Live Routing Results Dashboard — Faculty Demonstration

This edition replaces the old saved-experiment comparison panel with a live routing-results panel. The terminal-commands panel remains removed.

## Run

1. Stop the older dashboard with Ctrl+C and make sure TCP port 8080 is free.
2. From this `finalui` folder, run `sudo python3 ui/app.py` on an Ubuntu machine with Mininet, Open vSwitch and OS-Ken configured.
3. Visit `http://127.0.0.1:8080/?version=routing` in Firefox. If needed, press Ctrl+Shift+R.
4. Click **Start network**, choose **AUTO**, **Round Robin**, **SJF** or **Adaptive**, and click **Generate traffic**.
5. Scroll to **10 · Live request routing results — RR / SJF / Adaptive**.

## How to read Section 10

- Each mode card shows cumulative **new TCP flow assignments** emitted by the SDN controller while that scheduling mode was active, broken down by the selected backend server. These counts are reset when the network restarts.
- The recent timeline shows the source client, the effective algorithm at the time of assignment, and the chosen backend.
- The bottom summary displays **successful application replies** reported by the currently running client-load CSV files. It is NOT grouped by algorithm, because the existing client CSV does not record the mode for each completed request.
- In SJF mode the controller assigns the TCP connection to a server, and SJF prioritizes waiting **jobs inside that server**. Section 10 shows which server received the connection, not the internal order of queued jobs. The server cards in Section 05 display SJF-dispatch counters.
- An `assign` event indicates a controller forwarding decision, not necessarily that the request completed. Use the separate successful-response counters to explain the difference.

The controller's routing-selection logic and the three-way AUTO thresholds are unchanged. Network behavior must still be verified on your own Ubuntu/Mininet setup.
