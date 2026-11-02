"""Run the read-only web dashboard.

    python -m gateway.web [--config PATH]
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from gateway.config import load_config
from gateway.web.app import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway.web", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="path to config.yaml (default: ./config.yaml)")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    print(f"Dashboard: http://{cfg.web.host}:{cfg.web.port}/  (Ctrl+C to stop)")
    uvicorn.run(create_app(cfg), host=cfg.web.host, port=cfg.web.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
