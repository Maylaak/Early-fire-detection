"""Print what is in the database, using the web app's READ-ONLY connection.

    python -m gateway.status [--config PATH]
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

from gateway.config import load_config
from gateway.db import events, make_engine, nodes, readings, utcnow


def _ago(t) -> str:
    s = int((utcnow() - t).total_seconds())
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway.status", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="path to config.yaml (default: ./config.yaml)")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    engine = make_engine(cfg.database.web_url, read_only=True)

    try:
        with engine.connect() as conn:
            node_rows = conn.execute(select(nodes).order_by(nodes.c.name)).all()
            total = conn.execute(select(func.count()).select_from(readings)).scalar_one()
            counts = dict(conn.execute(
                select(readings.c.dev_eui, func.count()).group_by(readings.c.dev_eui)).all())
            recent_events = conn.execute(
                select(events).order_by(events.c.time.desc()).limit(5)).all()
    except (OperationalError, ProgrammingError) as e:
        print(f"Cannot read the database: {str(e.orig).strip().splitlines()[0]}")
        print("Has the detection service (python -m gateway.service) run at least once?")
        return 1

    print(f"Database: {make_url(cfg.database.web_url).render_as_string(hide_password=True)} (read-only)")
    print(f"{len(node_rows)} nodes, {total} readings stored\n")
    print(f"{'node':<8} {'dev_eui':<17} {'state':<7} {'last seen':<10} {'rssi':>5} {'snr':>6} "
          f"{'batt':>5} {'readings':>8}")
    for n in node_rows:
        print(f"{n.name:<8} {n.dev_eui:<17} {n.state:<7} {_ago(n.last_seen):<10} "
              f"{n.last_rssi if n.last_rssi is not None else '-':>5} "
              f"{n.last_snr if n.last_snr is not None else '-':>6} "
              f"{n.battery_v:>5.2f} {counts.get(n.dev_eui, 0):>8}")
    if recent_events:
        print("\nRecent events:")
        for e in recent_events:
            print(f"  {e.time:%Y-%m-%d %H:%M:%S}Z  {e.kind:<8} {e.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
