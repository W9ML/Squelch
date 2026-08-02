#!/usr/bin/env python3
"""Report traffic origin (local RF vs connected node) to squelch.

Runs on the AllStar node next to mdc_forward.py. Polls app_rpt through
Asterisk's AMI (the same RptStatus XStat/SawStat used by Allmon) and,
whenever the node starts passing audio, POSTs where it came from:

    local RF   -> RPT_RXKEYED=1 (the repeater's own receiver is open)
    a node     -> a connected node shows keyed in SawStat

    python3 source_monitor.py --node <your-node> --url http://<vm-ip>:8080

AMI credentials are read from /etc/asterisk/manager.conf automatically
(override with --user/--secret). Standard library only.
"""

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request


# ---------- parsers (kept pure for tests) ----------

def parse_manager_conf(path="/etc/asterisk/manager.conf"):
    """Return (username, secret) of the first user section."""
    user = secret = None
    section = None
    with open(path) as f:
        for line in f:
            line = line.split(";")[0].strip()
            if not line:
                continue
            m = re.match(r"\[(.+)\]", line)
            if m:
                sec = m.group(1)
                if sec != "general" and user is None:
                    section = sec
                else:
                    section = None if sec == "general" else section
                continue
            if section and "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                if k == "secret" and secret is None:
                    user, secret = section, v
    if not user or not secret:
        raise RuntimeError(f"no manager user/secret found in {path}")
    return user, secret


def parse_rxkeyed(block: str) -> bool:
    """XStat response -> is the local receiver keyed?"""
    m = re.search(r"RPT_RXKEYED=(\d)", block)
    return bool(m and m.group(1) == "1")


def parse_keyed_nodes(block: str) -> list[str]:
    """SawStat response -> node numbers currently keyed.
    Lines look like: 'Conn: <node> <isKeyed> <keyedSecs> <unkeyedSecs>'"""
    keyed = []
    for line in block.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "Conn:":
            if parts[2] == "1":
                keyed.append(parts[1])
    return keyed


def parse_connected_nodes(block: str) -> list[str]:
    """XStat response -> node numbers this node is currently linked to.

    Two formats are handled so this works across app_rpt versions:
      * RPT_ALINKS (the same all-links list Allmon reads):
            RPT_ALINKS=2,46655T,2001R
        the trailing letter is the link mode (T=transceive, R=monitor,
        C=connecting); nodes still connecting (C) aren't linked yet.
      * 'Conn:' lines, node in field 1, connection state last:
            Conn: 46655 1.2.3.4 0 OUT 1234 ESTABLISHED
        anything still CONNECTING is excluded.
    """
    m = re.search(r"RPT_ALINKS=\d+,(.*)", block)
    if m and m.group(1).strip():
        nodes = []
        for tok in m.group(1).split(","):
            mm = re.match(r"(\d+)([A-Za-z]?)", tok.strip())
            if mm and mm.group(2).upper() != "C":
                nodes.append(mm.group(1))
        return nodes
    nodes = []
    for line in block.splitlines():
        parts = line.split()
        if (len(parts) >= 2 and parts[0] == "Conn:" and parts[1].isdigit()
                and "CONNECTING" not in line.upper()):
            nodes.append(parts[1])
    return nodes


# ---------- AMI client ----------

class AMI:
    def __init__(self, host, port, user, secret, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        self._read_line()  # banner
        resp = self.request({"Action": "Login", "Username": user,
                             "Secret": secret, "Events": "off"})
        if "Success" not in resp:
            raise RuntimeError(f"AMI login failed: {resp.strip()}")

    def _read_line(self):
        while b"\r\n" not in self.buf:
            self.buf += self.sock.recv(4096)
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line.decode(errors="replace")

    def request(self, headers: dict) -> str:
        msg = "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
        self.sock.sendall(msg.encode())
        # responses end with a blank line
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("AMI closed")
            self.buf += chunk
        block, self.buf = self.buf.split(b"\r\n\r\n", 1)
        return block.decode(errors="replace")

    def rpt_status(self, command: str, node: str) -> str:
        return self.request({"Action": "RptStatus", "Command": command,
                             "Node": node})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------- forwarding ----------

def post_source(url, token, source, node=None, hub=None):
    body = json.dumps({"source": source, "node": node, "hub": hub}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/source", data=body,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-MDC-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except urllib.error.URLError as e:
        print(f"forward failed: {e}", file=sys.stderr, flush=True)


def post_link(url, token, connected, hub=None):
    """Report the currently-linked node list to squelch (gates voter polling +
    feeds the per-hub node-status roster)."""
    body = json.dumps({"connected": connected, "hub": hub}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/link", data=body,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-MDC-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except urllib.error.URLError as e:
        print(f"link report failed: {e}", file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--node", required=True, help="your app_rpt node number")
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--token", default=os.environ.get("SQUELCH_MDC_TOKEN", ""))
    ap.add_argument("--ami-host", default="127.0.0.1")
    ap.add_argument("--ami-port", type=int, default=5038)
    ap.add_argument("--user", default=None)
    ap.add_argument("--secret", default=None)
    ap.add_argument("--interval", type=float, default=0.4)
    ap.add_argument("--ignore", default="",
                    help="comma-separated node numbers to never report "
                         "(e.g. your squelch listener node)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if not args.user or not args.secret:
        args.user, args.secret = parse_manager_conf()
    ignore = {n.strip() for n in args.ignore.split(",") if n.strip()}

    print(f"source monitor: node {args.node} -> {args.url}/api/source",
          flush=True)
    active_source = None
    last_connected = None
    last_link_post = 0.0
    ami = None
    while True:
        try:
            if ami is None:
                ami = AMI(args.ami_host, args.ami_port, args.user, args.secret)
                print("AMI connected", flush=True)
            xstat = ami.rpt_status("XStat", args.node)
            sawstat = ami.rpt_status("SawStat", args.node)
            if args.debug:
                print("== XStat ==\n" + xstat + "\n== SawStat ==\n" + sawstat,
                      flush=True)
            local = parse_rxkeyed(xstat)
            remotes = [n for n in parse_keyed_nodes(sawstat) if n not in ignore]

            if local:
                src = ("local", None)
            elif remotes:
                src = ("node", remotes[0])
            else:
                src = None

            if src is not None and src != active_source:
                label = src[1] or "local RF"
                print(f"source: {label}", flush=True)
                post_source(args.url, args.token, src[0], src[1], hub=args.node)
            active_source = src

            # report the linked-node list so squelch can gate voter polling —
            # on change, plus a 30s heartbeat so it can tell we're still alive
            connected = sorted(n for n in parse_connected_nodes(xstat)
                               if n not in ignore)
            nowm = time.monotonic()
            if connected != last_connected or nowm - last_link_post > 30:
                if connected != last_connected:
                    print(f"linked nodes: {', '.join(connected) or 'none'}",
                          flush=True)
                post_link(args.url, args.token, connected, hub=args.node)
                last_connected = connected
                last_link_post = nowm

            time.sleep(args.interval)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"AMI error: {e}; reconnecting in 5s", file=sys.stderr,
                  flush=True)
            if ami:
                ami.close()
            ami = None
            active_source = None
            last_connected = None      # re-report link state on recovery
            time.sleep(5)


if __name__ == "__main__":
    main()
