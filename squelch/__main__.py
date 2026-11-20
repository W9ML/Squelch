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
    parser.add_argument("--reset-2fa", metavar="USERNAME",
                        help="turn off two-factor for an account that lost "
                             "its authenticator, then exit")
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

    if args.reset_2fa:
        # lock-out recovery for someone who lost their authenticator and has
        # no other super admin to reset it: run on the server, as the
        # service user, then log in with the password alone and re-enrol
        from .auth import AuthManager
        from .db import Database
        db = Database(cfg.db_path)
        if db.get_user_hash(args.reset_2fa) is None:
            print(f"no such user: {args.reset_2fa}", file=sys.stderr)
            return 1
        auth = AuthManager(db, cfg.data_dir)
        auth.clear_2fa(args.reset_2fa)
        auth.revoke_user(args.reset_2fa)
        db.audit("2fa_reset", None, None, f"{args.reset_2fa} (from the CLI)")
        print(f"two-factor cleared for {args.reset_2fa}; "
              "their sessions were ended")
        return 0

    app = create_app(cfg)
    tls = {}
    if cfg.tls_cert or cfg.tls_key:
        if not (cfg.tls_cert and cfg.tls_key):
            print("[web] tls_cert and tls_key must be set together",
                  file=sys.stderr)
            return 1
        tls = {"ssl_certfile": cfg.tls_cert, "ssl_keyfile": cfg.tls_key}
    elif cfg.web_bind not in ("127.0.0.1", "::1", "localhost"):
        logging.warning(
            "serving plain HTTP on %s:%d — put a TLS proxy in front (and set "
            "[web] force_https), or set [web] tls_cert / tls_key, before "
            "anyone logs in over an untrusted network",
            cfg.web_bind, cfg.web_port)
    # proxy_headers off: uvicorn must not rewrite the client address from
    # X-Forwarded-For. Squelch does that itself, only for [web]
    # trusted_proxies (see web.ProxyTrust).
    uvicorn.run(app, host=cfg.web_bind, port=cfg.web_port,
                log_level="warning", proxy_headers=False,
                server_header=False, **tls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
