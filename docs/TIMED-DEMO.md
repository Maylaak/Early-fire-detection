# Timed fire demo

Start the system with the updated `start_all.bat`. In a fourth project CMD run:

```bat
fire node-1 60
```

There is no token prompt or virtual-environment activation step. The wrapper
uses the project Python and DEMO_SPEEDUP=60. Keep this CMD open until completion.
The equivalent Python command is `python -m gateway.sim.timed_demo node-1 --seconds 60`.

The Windows launcher enables WILDFIRE_LOCAL_DEMO=1. Service/web automatically
share a random credential encrypted with Windows DPAPI for the current user;
the CLI loads it silently. It reuses the existing encrypted repair credential
when present; otherwise the file lives under LOCALAPPDATA/EarlyFireDetection.
No secret is embedded in source or public APIs, and remote unauthenticated clear
requests remain rejected. An explicitly configured WILDFIRE_ADMIN_TOKEN takes
precedence and must match across processes. Manual browser clearing remains an
independent authenticated action; the timed command needs no browser interaction.
For this upgrade, existing service/web processes without local-demo authorization
must be restarted once using the updated launcher. Do not run duplicate services.
The launcher refuses to start when gateway processes are already running.

`--seconds` is total **real elapsed time from injection**, not simulated minutes
and not time since the ALARM appeared. Smoke ramps are included in the duration.
Detection confirmation, duty cycle, persistence and dashboard polling mean the
visible alarm starts later and clears shortly after the deadline. Warm up the
simulator first. A very short fire may not satisfy detector confirmation.

The controller refuses to inject if a node already has an alarm. It records the
first observed alarm event ID and clears only that ID at the deadline. A newer
alarm, failed authorization, unavailable service or interrupted controller leaves
the alarm for manual review. No alarm is fabricated if local detection never
triggers. The timer is not persisted across closing this CMD or computer restart.

This is explicitly an operator-authorized demonstration reset, not evidence of
physical fire extinction. The clear is recorded by the existing MANUAL_CLEAR
audit pathway; original events and readings remain. Ordinary `gateway.sim inject`
commands, heartbeats and physical alerts still never clear an alarm automatically.
The local detector returns to normal through its existing hysteresis; the clear
does not reset firmware or manufacture a second alert during a sustained episode.

Verification: `python -m pytest tests/test_timed_demo.py tests/test_manual_clear.py
tests/test_event_simulator.py -q` with external database/MQTT fixtures disabled.
