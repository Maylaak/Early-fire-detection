# Demo runbook (Command Prompt, speed-up x60, event-driven nodes)

**The night before:** run the whole thing once with internet on. That way the Docker images are already downloaded (see section 6).

**How the demo behaves now.** Each node checks its own air every second (at x60) and sends nothing but a short heartbeat, until it detects a fire itself. Then it sends one **FIRE ALERT**. The console turns red and **stays red until the alarm is cleared**; it never clears on its own. The timed fire command (section 3) injects smoke and clears the alarm for you after the time you choose.

**Before you start:** close every old service, simulator and web window (Ctrl+C). There must be only one of each.

## 1. Start everything: one window each, in this order

**Window 1: Docker and the detection service**
```bat
start "" "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe"
ping -n 61 127.0.0.1 >nul
cd /d %USERPROFILE%\OneDrive\Desktop\vip
docker compose up -d --wait
C:\venvs\vip\Scripts\activate
set DEMO_SPEEDUP=60
set WILDFIRE_LOCAL_DEMO=1
python -m gateway.service
```
- **You should see:** `MQTT connected to localhost:1883`, then `detection service running`.
- **If not:** `cannot connect to the docker API` means Docker isn't ready. Wait for "Engine running" in Docker Desktop and paste the block again. `database unavailable` repeating means Postgres is still starting; it retries on its own.

**Window 2: the simulator (9 nodes)**
```bat
cd /d %USERPROFILE%\OneDrive\Desktop\vip
C:\venvs\vip\Scripts\activate
set DEMO_SPEEDUP=60
python -m gateway.sim run
```
- **You should see:** `simulating 9 nodes: heartbeat every 1440s, sensor read every 1s`, then one `HEARTBEAT` line per node. After that the window is quiet. That's normal: nodes only transmit for a fire, or every 24 minutes.

**Window 3: the web console**
```bat
cd /d %USERPROFILE%\OneDrive\Desktop\vip
C:\venvs\vip\Scripts\activate
set DEMO_SPEEDUP=60
set WILDFIRE_LOCAL_DEMO=1
python -m gateway.web
start http://127.0.0.1:8000/
```
- **You should see:** the console with **ALL CLEAR** and 9 nodes, all NORMAL. Click **Sound** once to arm it.
- **If the header shows `{{centre_title}}`:** an old web window is still running. Close it and start this one again.

## 2. Warm-up

Each node needs 30 readings of its own before it can detect anything. At x60 that's **30 seconds** after the simulator starts (30 minutes in real time).

The console can't show this: it says ALL CLEAR straight away, because the heartbeats arrive at once. **Wait at least one minute after starting window 2 before the first fire.**

## 3. Trigger a fire (window 4)

```bat
cd /d %USERPROFILE%\OneDrive\Desktop\vip
C:\venvs\vip\Scripts\activate
set DEMO_SPEEDUP=60
python -m gateway.sim.timed_demo node-3 --seconds 60
```

Shorter, same thing: `fire node-3` (60 s by default; `fire node-3 90` for 90 s). `fire.bat` finds `C:\venvs\vip` by itself and sets the speed-up.

`--seconds` is how long the whole demo lasts, counted from the injection. Measured on a test run:

| Time after you press Enter | What happens |
|---|---|
| ~13 s | window 2 prints `node-3 ... FIRE_ALERT`, window 1 prints `ALERT F-07-03: explicit FIRE ALERT from node` |
| ~15–20 s | console turns red: **FIRE ALARM**, F-07-03 |
| 60 s | window 4 prints `alarm clear confirmed`; console back to **ALL CLEAR** within ~5 s |

- Keep window 4 open until it prints `Timed demo complete`. If you close it early, the alarm stays on.
- **60 seconds** is the presentation length. Use 90 if you need more time to talk. Below 30 the alarm is barely on screen; the minimum is 15.
- The alarm takes about the same time on every node, near or far (one short message, no radio slowdown). Any of node-1 … node-9 works.
- No WATCH stage appears in this mode: the node decides, and the console goes straight from ALL CLEAR to FIRE ALARM.
- The node charts stay empty, because nodes no longer send routine readings. Show the map and the **Sensor Network** panel instead: press GW-01 → cluster F-07 → the node in alarm. This works on the map or in the panel, and both follow: pressing a gateway zooms to its sector and shows its clusters, pressing a cluster zooms to its nodes. **‹ All gateways** (top left of the map) goes back. Press a gateway a second time for its details.
- **Drone (simulated):** when the alarm appears, the drone panel says SIMULATION and the drone icon flies from its base in the middle of the reserve to the node, along a dashed red line (about 600 m, ≈ 40 s real flight, shown over a few seconds). When the alarm clears it flies back. Say clearly that there is no real drone link yet.
- **Map:** the yellow outline is the reserve; dotted lines split it into five gateway sectors, and dashed circles show each gateway's theoretical range (524 m, see `docs/GATEWAY-COVERAGE.md`). Only GW-01 has nodes; GW-02 to GW-05 show as **planned**. Switch the circles off under **Layers** if the map looks busy.

**Don't use `python -m gateway.sim inject ...` on its own:** that alarm stays on until someone clears it (see section 4).

## 4. Run it again, or reset

- **Again:** wait until window 4 says `Timed demo complete`, then run the same command again, on the same node or another one. Tested: the same node alarmed again about 2 minutes later without trouble.
- **The timed command refuses** ("This node already has an alarm"): an alarm is still on for that node. Use another node, or reset.
- **Clean reset** (deletes all stored data). Press Ctrl+C in windows 1, 2 and 3, then in window 1:
  ```bat
  docker compose down -v
  del data\simulator-state.sqlite
  docker compose up -d --wait
  python -m gateway.service
  ```
  Then paste the window 2 and window 3 blocks again and wait one minute.

## 5. The most likely failures in the room

1. **The timed command says "Restart service/web using the updated start_all.bat" or "Operator authorization ... failed".** Window 1 or 3 was started without `set WILDFIRE_LOCAL_DEMO=1`. In that window: Ctrl+C, run the `set` line, start it again.
2. **"Command and running dashboard must use the same DEMO_SPEEDUP".** One window is missing `set DEMO_SPEEDUP=60`. Fix that window the same way.
3. **"No fire alarm was detected during the demo".** You fired too soon after starting the simulator, or with `--seconds` too short. Wait a minute and run it again with 60 or more.
4. **Docker or the service died** (laptop slept). The console shows NO DATA. Start Docker Desktop, run `docker compose up -d --wait` in window 1, then restart windows 1, 2 and 3. Alarms and history survive a restart.

## 6. Without internet

- **Still works:**
  - broker, database, detection, simulator, timed fire;
  - the whole console: markers, panel, simulated drone, sound (map code is bundled);
  - alerts in window 1.
- **Breaks:**
  - **Map background:** it goes grey with "No map imagery (no internet?)", but markers stay in place.
  - **Telegram and email alerts** (if enabled): they fail, and window 1 logs the error.
  - The very first `docker compose up` on a new machine, and `pip install`.

*Note: `fire.bat` finds `C:\venvs\vip` and can replace section 3. `start_all.bat` still expects a `.venv` folder inside the project, so use the section 1 commands to start.*
