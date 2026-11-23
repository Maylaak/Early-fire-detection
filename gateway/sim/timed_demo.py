"""Opt-in operator-authorized timed fire demonstration (real seconds).

Keep this command running until it confirms the clear. Normal injections and
real node alarms remain latched; no new radio message or web write API is added.
"""
import argparse
from dataclasses import replace
import logging
import math
import time

import httpx

from gateway.commands import CommandSettings, forward_clear
from gateway.config import load_config
from gateway.local_operator import local_operator_token
from gateway.sim.__main__ import _find_node, cmd_inject


def wait_and_clear(seconds, status, clear, *, clock=time.monotonic, sleep=time.sleep):
    """Capture one new alarm; never clear a different, newer alarm episode."""
    deadline = clock() + seconds
    alarm_id = None
    while True:
        node = status()
        if node and node['state'] == 'ALARM':
            current = node.get('alarm_event_id')
            if not isinstance(current, int):
                raise RuntimeError('Alarm has no persisted event ID; clear not confirmed.')
            if alarm_id is None:
                alarm_id = current
                print('Fire alarm detected. Waiting for the configured demo duration.', flush=True)
            elif current != alarm_id:
                raise RuntimeError('A different alarm arrived; leaving it active for operator review.')
        if clock() >= deadline:
            break
        sleep(min(1, max(0, deadline - clock())))
    if alarm_id is None:
        raise RuntimeError('No fire alarm was detected during the demo. Check warm-up and simulator output.')
    if not node or node['state'] != 'ALARM':
        print('The demo alarm is already cleared; no additional clear sent.')
        return
    code, result = clear(alarm_id)
    if code != 200 or not result.get('ok'):
        raise RuntimeError(result.get('detail', 'Clear not confirmed; inspect dashboard.'))
    print('Timed demo complete: alarm clear confirmed; history preserved.', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('node', help='e.g. node-1')
    parser.add_argument('--seconds', required=True, type=float,
                        help='total real seconds from injection, including smoke ramps')
    parser.add_argument('--config')
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 15 <= args.seconds <= 86400:
        parser.error('--seconds must be finite and between 15 and 86400')
    cfg = load_config(args.config)
    node = _find_node(cfg, args.node)
    if node is None:
        parser.error('Unknown simulator node')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{cfg.web.port}', timeout=5,
                          trust_env=False) as client:
            def get(path):
                response = client.get(path)
                response.raise_for_status()
                return response.json()

            def status():
                return next((n for n in get('/api/console/status')['nodes']
                             if n['dev_eui'] == node.dev_eui), None)

            public = get('/api/console/config')
            if not public.get('clear_alarm_enabled'):
                raise RuntimeError('Restart service/web using the updated start_all.bat to enable automatic timed demos.')
            if public.get('reporting_mode') != 'event_driven' or public['demo_speedup'] != cfg.timing.demo_speedup:
                raise RuntimeError('Command and running dashboard must use the same DEMO_SPEEDUP and event-driven mode.')
            before = status()
            if before and before['state'] == 'ALARM':
                raise RuntimeError('This node already has an alarm. Clear it explicitly before starting a timed demo.')
            settings = CommandSettings.from_env()
            if not settings.token:
                settings = replace(settings, token=local_operator_token())
            # Authenticate against a deliberately invalid identifier: validation
            # follows authorization, and this cannot target any real node.
            code, _ = forward_clear(settings, 'timed-demo-auth-check', 1)
            if code != 400:
                raise RuntimeError('Operator authorization/service check failed; no fire injected.')
            ramp_seconds = min(5, args.seconds / 4)
            scale = cfg.timing.demo_speedup / 60
            command = argparse.Namespace(node=node.name,
                hold_minutes=(args.seconds - 2 * ramp_seconds) * scale,
                ramp_minutes=ramp_seconds * scale)
            if cmd_inject(cfg, command):
                raise RuntimeError('Smoke injection failed; no clear scheduled.')
            print(f'Timed demo: {args.seconds:g} real seconds. Keep this CMD open.', flush=True)
            wait_and_clear(args.seconds, status,
                           lambda alarm_id: forward_clear(settings, node.dev_eui, alarm_id))
        return 0
    except (RuntimeError, ValueError, httpx.HTTPError) as exc:
        print(f'Demo not completed: {exc}')
        return 1
    except KeyboardInterrupt:
        print('Timer cancelled. Any recorded alarm remains latched; clear it manually.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
