#!/usr/bin/env python3
"""Forward app_rpt MDC-1200 decodes to squelch.

Runs on the AllStar node (the Pi). Tails the file app_rpt writes via
`mdclog=` in rpt.conf and POSTs each newly decoded burst to squelch's
/api/mdc endpoint, which attaches it to the matching transmission.

Standard library only — no pip installs on the node.

    python3 mdc_forward.py \
        --log /var/log/asterisk/mdc.log \
        --url http://<vm-ip>:8080 \
        [--token SECRET]

It starts at the END of the log (does not replay history) and follows
across log rotation/truncation, so squelch can use its own arrival time
for matching — no clock sync between node and VM required.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# "YYYYMMDDhhmmss <node> <TYPE><unitid>"  TYPE: I/E/S/C
LINE_RE = re.compile(r"^(\d{14})\s+(\d+)\s+([IESC])(\S+)\s*$")


def post_event(url: str, token: str, node: str, mtype: str, unit: str) -> None:
    body = json.dumps({"unit": unit, "type": mtype, "node": node}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/mdc", data=body,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-MDC-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except urllib.error.URLError as e:
        print(f"forward failed: {e}", file=sys.stderr)


def follow(path: str):
    """Yield new lines appended to `path`, tolerating rotation."""
    while not os.path.exists(path):
        time.sleep(1.0)
    f = open(path, "r")
    f.seek(0, os.SEEK_END)          # start at end: only new bursts
    squelch = os.fstat(f.fileno()).st_ino
    while True:
        line = f.readline()
        if line:
            yield line
            continue
        time.sleep(0.4)
        try:
            if os.stat(path).st_ino != squelch or \
                    os.stat(path).st_size < f.tell():
                # rotated or truncated: reopen from the top
                f.close()
                f = open(path, "r")
                squelch = os.fstat(f.fileno()).st_ino
        except FileNotFoundError:
            time.sleep(1.0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", default="/var/log/asterisk/mdc.log",
                    help="path to app_rpt's mdclog file")
    ap.add_argument("--url", default="http://127.0.0.1:8080",
                    help="base URL of the squelch web server")
    ap.add_argument("--token", default=os.environ.get("SQUELCH_MDC_TOKEN", ""),
                    help="shared secret (matches [mdc] forward_token)")
    args = ap.parse_args()

    print(f"forwarding MDC from {args.log} -> {args.url}/api/mdc", flush=True)
    for line in follow(args.log):
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        _ts, node, mtype, unit = m.groups()
        print(f"MDC {mtype}{unit} (node {node})", flush=True)
        post_event(args.url, args.token, node, mtype, unit)


if __name__ == "__main__":
    main()
