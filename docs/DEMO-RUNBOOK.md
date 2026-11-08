# Demo runbook (Command Prompt, speed-up x60)

**The night before:** run the whole thing once with internet on. That way the Docker images are already cached (see 7).

## 1–2. Bring it up from cold, and what you should see

| # | Where | Type | You should see | If not |
|---|---|---|---|---|
| 1 | Window A | `start "" "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe"` | Docker Desktop opens; wait for **Engine running** (about 1 min) | Start it from the Start menu. Wait. |
| 2 | A | `cd /d %USERPROFILE%\OneDrive\Desktop\vip` | prompt ends in `\vip>` | |
| 3 | A | `docker compose up -d` | `Container ...postgres... Started`, `...mosquitto... Started` | "cannot connect to the docker API": the engine isn't up yet. Wait and repeat. |
| 4 | A | `docker compose ps` | both `Up`, postgres `(healthy)` | `(starting)`: wait 10 s and repeat |
| 5 | Windows B, C, D, each: | `cd /d %USERPROFILE%\OneDrive\Desktop\vip`<br>`C:\venvs\vip\Scripts\activate`<br>`set DEMO_SPEEDUP=60` | prompt starts with `(vip)` | `python` not found: you skipped `activate` |
| 6 | B | `python -m gateway.config` | a line `demo speed-up       x60` | x1: redo `set DEMO_SPEEDUP=60` |
| 7 | B | `python -m gateway.service` | `MQTT connected to localhost:1883`, then `detection service running` | `cannot reach MQTT broker`: go back to step 4. `database unavailable` repeating: Postgres isn't healthy yet; it retries on its own. |
| 8 | C | `python -m gateway.sim run` | `simulating 4 nodes: heartbeat every 10s`, then one line per uplink | `cannot reach MQTT broker`: step 4 |
| 9 | D | `python -m gateway.web` | `Dashboard: http://127.0.0.1:8000/` | port in use: an old web window is still open; close it |
| 10 | Browser | open `http://127.0.0.1:8000/` and click **Sound** once to arm it | pill **WARMING UP**, 4 nodes in the table | "cannot reach the database": step 7 must be running first |

Start in this order: service (B), then simulator (C), then web (D).

## 3. Warm-up (each node needs 30 readings)

| `DEMO_SPEEDUP` | 1 (real) | 30 | **60** | 120 (never rehearsed) |
|---|---|---|---|---|
| Warm-up | 4 h 50 min | ~10 min | **~5 min** | ~2.5 min |

**Ready when:**
- the pill changes from **WARMING UP** to **ALL CLEAR**;
- the "learning baselines" note disappears;
- window B has printed `warm-up complete, monitoring` once for each of the 4 nodes.

**Don't inject smoke before the pill says ALL CLEAR.** A node that is still warming up cannot alarm.

## 4. Inject smoke

In window D, or any window prepared as in step 5:

```bat
python -m gateway.sim inject node-3
```

- node-3 is site **F-07-03**. Window C prints `>>> SMOKE EVENT injected into node-3`.
- Timings at x60, measured on the simulator. The console refreshes every 5 s, so allow up to 5 s more on screen.
  - **WATCH:** about 5 s.
  - **FIRE ALARM, Block F-07:** about 8 s.
  - **Back to ALL CLEAR:** about 35 s after injecting.
- Real time (x1) is 60 times slower: WATCH about 5 min, ALARM about 8 min.
- Use node-1, node-2 or node-3. Avoid **node-4**: it's at SF11, so it's about twice as slow.

## 5. Run it twice

- **Quick (no reset):** wait until the pill is back to ALL CLEAR (about 35 s), then inject again, ideally into node-1.
- **Clean reset:** press Ctrl+C in windows B, C and D, then in window A:

  ```bat
  docker compose down -v
  docker compose up -d
  ```

  Then repeat steps 7–10. `-v` **deletes all stored data and the broker's queue**; that's what you want here. Warm-up starts again, about 5 min.

## 6. The three most likely failures in the room

1. **Nodes flip to MISSING, or a yellow "Setup problem" bar appears.** One window is missing `DEMO_SPEEDUP=60`.
   - In that window: Ctrl+C, `set DEMO_SPEEDUP=60`, then start it again.
   - Check every window with `python -m gateway.config`.
2. **You inject and nothing happens.**
   - Was the pill still WARMING UP? Then wait for ALL CLEAR and inject again.
   - Does window C print `SMOKE EVENT`? If not, the simulator isn't running: restart it (step 8).
   - Still nothing: inject into `node-1`.
3. **Docker or the service dies** (laptop slept, or Docker Desktop restarted).
   - The console shows **NO DATA** or "cannot reach the database".
   - Start Docker Desktop, then `docker compose up -d` in window A, then restart B, C and D.
   - Baselines come back from the database: no new warm-up if the gap was under ~12 min.
   - Talking point while it recovers: alerts don't depend on the web page. Telegram and the service window keep working.

## 7. Without internet

- **Still works:**
  - broker, database, detection, simulator;
  - the operator console: page, markers, zones, event log, charts and sound (Leaflet is bundled);
  - alerts in the service window.
- **Breaks:**
  - **Map imagery.** The map background goes grey with "No map imagery (no internet?)", but markers stay in the right place.
  - **Telegram alerts** (only if you enabled them). They're retried, then the event log shows them as failed. Console alerts still go out.
  - **First-ever `docker compose up`** (it needs to download the images) and `pip install`.
