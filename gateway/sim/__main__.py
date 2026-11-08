"""Simulator command line.

    python -m gateway.sim run [--interval SECONDS] [--seed N] [--dry-run]
    python -m gateway.sim inject NODE [--hold-minutes M]

"run" reads every node's simulated sensor every simulator.firmware.sample_seconds
and publishes a ChirpStack v4 uplink when the PLACEHOLDER firmware would
transmit: a heartbeat every timing.heartbeat_minutes, or at once and then
every minute while the node's local gas trigger holds and for a
while after (duty cycle permitting; simulator.firmware in config.yaml).
See gateway/sim/firmware.py.
"inject" tells an already-running simulator (via MQTT) to start a smoke
event on NODE (a node name like node-2, or its dev_eui).

With DEMO_SPEEDUP set, every duration (including inject's minutes) is divided by it.
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import random
import sys
import time
from datetime import datetime, timezone

from gateway.chirpstack import build_uplink, uplink_topic
from gateway.codec import encode
from gateway.config import Config, SimNode, load_config
from gateway.mqtt import BrokerUnavailable, connect
from gateway.sim.firmware import ALARM_MODE, HEARTBEAT, NodeFirmware, Transmission
from gateway.sim.model import NodeModel, Sample

log = logging.getLogger("gateway.sim")


def _find_node(cfg: Config, key: str) -> SimNode | None:
    key = key.lower()
    for n in cfg.simulator.nodes:
        if key in (n.name.lower(), n.dev_eui):
            return n
    return None


def _node_names(cfg: Config) -> str:
    return ", ".join(n.name for n in cfg.simulator.nodes)


def _envelope(cfg: Config, model: NodeModel, s: Sample, reason: str = HEARTBEAT) -> dict:
    sim, node, ports = cfg.simulator, model.node, cfg.lorawan
    return build_uplink(
        application_id=sim.application_id,
        application_name=sim.application_name,
        device_name=node.name,
        dev_eui=node.dev_eui,
        dev_addr=node.dev_addr,
        f_cnt=s.f_cnt,
        # The port tells the gateway which mode the node is in.
        f_port=ports.alarm_mode_f_port if reason == ALARM_MODE else ports.heartbeat_f_port,
        payload=encode(s.reading),
        rssi=s.rssi,
        snr=s.snr,
        time=s.time,
        gateway_id=sim.gateway_id,
        spreading_factor=model.spreading_factor,
    )


def _log_sample(model: NodeModel, tx: Transmission) -> None:
    s, r = tx.sample, tx.sample.reading
    tags = [tx.reason]
    if s.smoke_level > 0:
        tags.append(f"SMOKE {s.smoke_level:.0%}")
    if s.raining:
        tags.append("rain")
    log.info(
        "%-7s fCnt=%-5d T=%6.2fC RH=%5.1f%% P=%7.1fhPa gas=%7.1fkOhm batt=%.2fV "
        "rssi=%4d snr=%5.1f SF%d %s",
        model.node.name, s.f_cnt, r.temperature_c, r.humidity_pct, r.pressure_hpa,
        r.gas_resistance_ohm / 1000, r.battery_v, s.rssi, s.snr, model.spreading_factor,
        " ".join(tags),
    )


def _handle_command(raw: bytes, models: dict[str, NodeModel], cfg: Config) -> None:
    k = cfg.timing.demo_speedup
    try:
        cmd = json.loads(raw)
        node = _find_node(cfg, str(cmd["node"]))
    except (ValueError, KeyError, TypeError):
        log.warning("ignoring malformed control message: %r", raw[:200])
        return
    if node is None:
        log.warning("inject: unknown node %r", cmd.get("node"))
        return
    smoke = cfg.simulator.smoke
    hold, ramp = cmd.get("hold_minutes"), cmd.get("ramp_minutes")
    try:
        # Command-line minutes are simulated minutes, like the config's.
        hold = None if hold is None else float(hold) / k
        ramp = None if ramp is None else float(ramp) / k
        models[node.name].inject_smoke(datetime.now(timezone.utc), hold, ramp)
    except (TypeError, ValueError):
        log.warning("inject: bad hold/ramp minutes in %r", cmd)
        return
    log.warning(">>> SMOKE EVENT injected into %s (ramp %.3g min, hold %.3g min wall clock) <<<",
                node.name, ramp if ramp is not None else smoke.ramp_minutes,
                hold if hold is not None else smoke.hold_minutes)


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    sim = cfg.simulator
    # Heartbeat from timing (already divided by demo_speedup); --interval overrides.
    heartbeat_min = args.interval / 60 if args.interval is not None else cfg.timing.heartbeat_minutes
    seed = args.seed if args.seed is not None else sim.seed
    start = datetime.now(timezone.utc)
    nodes = {
        n.name: NodeFirmware(
            NodeModel(
                n, sim,
                random.Random(f"{seed}:{n.dev_eui}") if seed is not None else random.Random(),
                start,
            ),
            sim.firmware, heartbeat_min, cfg.timing.demo_speedup,
        )
        for n in sim.nodes
    }
    models = {name: fw.model for name, fw in nodes.items()}
    interval = sim.firmware.sample_seconds

    # Control messages arrive on paho's network thread; hand them to the main loop.
    commands: queue.Queue[bytes] = queue.Queue()
    client = None
    if not args.dry_run:
        try:
            client = connect(cfg.mqtt, "wildfire-sim", [sim.control_topic],
                             lambda msg: commands.put(msg.payload))
        except BrokerUnavailable as e:
            log.error("%s", e)
            return 1

    log.info("simulating %d nodes: heartbeat every %gs, sensor read every %gs%s. Ctrl+C to stop.",
             len(nodes), heartbeat_min * 60, interval,
             " (dry run, printing only)" if args.dry_run else "")
    for name, fw in nodes.items():
        limited = fw.duty_cycle_limited()
        if limited:
            log.info("%s is at SF%d: the duty cycle allows one uplink per %.0fs, so it sends %s",
                     name, fw.model.spreading_factor, fw.min_gap.total_seconds(), ", ".join(
                         f"{rate} every {every.total_seconds():g}s (configured "
                         f"{fw.configured_rates()[rate].total_seconds():g}s)"
                         for rate, every in limited.items()))
    next_tick = time.monotonic()
    sent = 0
    try:
        while True:
            now_m = time.monotonic()
            if now_m >= next_tick:
                now = datetime.now(timezone.utc)
                for fw in nodes.values():
                    tx = fw.step(now)
                    if tx is None:
                        continue
                    env = _envelope(cfg, fw.model, tx.sample, tx.reason)
                    if client is None:
                        print(json.dumps(env))
                    else:
                        client.publish(uplink_topic(sim.application_id, fw.model.node.dev_eui),
                                       json.dumps(env), qos=cfg.mqtt.qos)
                    _log_sample(fw.model, tx)
                    sent += 1
                next_tick += interval
                if next_tick < now_m:  # fell behind (e.g. laptop slept): don't burst
                    next_tick = now_m + interval
                if args.count is not None and sent >= args.count:
                    return 0
                continue
            try:
                raw = commands.get(timeout=next_tick - now_m)
            except queue.Empty:
                continue
            _handle_command(raw, models, cfg)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0
    finally:
        if client is not None:
            client.loop_stop()
            client.disconnect()


def cmd_inject(cfg: Config, args: argparse.Namespace) -> int:
    node = _find_node(cfg, args.node)
    if node is None:
        log.error("unknown node %r. Known nodes: %s", args.node, _node_names(cfg))
        return 1
    try:
        client = connect(cfg.mqtt, "wildfire-sim-inject")
    except BrokerUnavailable as e:
        log.error("%s", e)
        return 1
    body = {"node": node.name}
    if args.hold_minutes is not None:
        body["hold_minutes"] = args.hold_minutes
    if args.ramp_minutes is not None:
        body["ramp_minutes"] = args.ramp_minutes
    try:
        info = client.publish(cfg.simulator.control_topic, json.dumps(body), qos=1)
        info.wait_for_publish(timeout=5)
        if not info.is_published():
            log.error("broker did not acknowledge the inject command")
            return 1
    finally:
        client.loop_stop()
        client.disconnect()
    log.info("smoke injection sent to %s. Watch the simulator window.", node.name)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway.sim", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="path to config.yaml (default: ./config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="publish simulated uplinks")
    run.add_argument("--interval", type=float,
                     help="seconds between heartbeats (overrides timing.heartbeat_minutes; "
                          "prefer DEMO_SPEEDUP for demos)")
    run.add_argument("--seed", type=int, help="random seed for a repeatable run")
    run.add_argument("--dry-run", action="store_true", help="print envelopes, no MQTT broker needed")
    run.add_argument("--count", type=int, help="stop after this many uplinks")

    inj = sub.add_parser("inject", help="start a smoke event on a running simulator")
    inj.add_argument("node", help="node name (e.g. node-2) or dev_eui")
    inj.add_argument("--hold-minutes", type=float,
                     help="minutes at full strength (overrides config; divided by DEMO_SPEEDUP)")
    inj.add_argument("--ramp-minutes", type=float,
                     help="minutes to reach full strength (overrides config; divided by DEMO_SPEEDUP)")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    if args.command == "run" and args.interval is not None and args.interval <= 0:
        parser.error("--interval must be positive")
    if args.command == "inject":
        if args.ramp_minutes is not None and args.ramp_minutes <= 0:
            parser.error("--ramp-minutes must be positive")
        if args.hold_minutes is not None and args.hold_minutes < 0:
            parser.error("--hold-minutes cannot be negative")
    cfg = load_config(args.config)
    return cmd_run(cfg, args) if args.command == "run" else cmd_inject(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
