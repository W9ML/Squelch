"""Entry point: python -m squelch --config /etc/squelch/squelch.toml"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from .config import load_config
from .web import create_app


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="squelch",
        description="AllStar node monitor: transcription, voice ID, MDC1200")
    parser.add_argument("-c", "--config", default="/etc/squelch/squelch.toml",
                        help="path to config file (TOML)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"config file not found: {cfg_path}", file=sys.stderr)
        print("copy config.example.toml there and edit it", file=sys.stderr)
        return 1

    cfg = load_config(cfg_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    # admin_password bootstraps the first "admin" account on an empty
    # database; once any user exists, accounts are managed in the web UI
    if not cfg.admin_password and not (cfg.db_path.exists()):
        logging.warning("no admin_password set and no users exist yet - "
                        "admin features will be locked until you set one")

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.web_bind, port=cfg.web_port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
