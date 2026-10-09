"""Command line entry point: ``pitlane run`` / ``pitlane check-config``."""

from __future__ import annotations

import argparse
import os
import sys

from .config import ConfigError, config_warnings, load_config, split_bind
from .logging import get_logger, setup_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pitlane", description="Pitlane – motorsport sources to Plex")
    ap.add_argument("--config", "-c", default=os.environ.get("PITLANE_CONFIG", "/config/pitlane.yaml"))
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the service (default)")
    sub.add_parser("check-config", help="validate the configuration file and exit")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        # OPS-3: refuse to start, print the exact path and reason.
        print(f"pitlane: invalid configuration in {args.config}:", file=sys.stderr)
        for err in e.errors:
            print(f"  {err}", file=sys.stderr)
        return 2

    setup_logging(cfg.server.log_level)
    log = get_logger("cli")
    if args.cmd == "check-config":
        for w in config_warnings(cfg):
            print(f"warning: {w}", file=sys.stderr)
        print("configuration OK")
        return 0

    import uvicorn

    from .app import create_app

    host, port = split_bind(cfg.server.bind)
    api = create_app(cfg)
    log.info("listening", extra={"bind": cfg.server.bind})
    uvicorn.run(api, host=host or "0.0.0.0", port=port, log_config=None, access_log=False, lifespan="on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
