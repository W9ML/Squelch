#!/usr/bin/env python3
"""Reference client for an encrypted voter SSE stream.

Connects to a /api/nodes/<id>/voter endpoint, decrypts each event with
the shared AES-128-CTR key, and prints the voted receiver each update.
Handy for confirming a key + URL work before wiring them into Squelch.

    python3 tools/voter_client.py \
        --url https://host/api/nodes/46655/voter \
        --key <32 hex chars>

Standalone apart from `requests` and `cryptography` (both Squelch deps).
"""

import argparse
import base64
import json
import sys

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def decrypt(data_field: str, key: bytes) -> str:
    iv_b64, sep, ct_b64 = data_field.partition(".")
    if not sep:
        raise ValueError("not an encrypted payload")
    iv = base64.b64decode(iv_b64)
    ct = base64.b64decode(ct_b64)
    dec = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    return (dec.update(ct) + dec.finalize()).decode("utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True, help="32 hex chars")
    ap.add_argument("--raw", action="store_true", help="print full JSON")
    args = ap.parse_args()

    key = bytes.fromhex(args.key.strip())
    if len(key) != 16:
        sys.exit("key must be 16 bytes (32 hex characters)")

    print(f"connecting to {args.url} ...")
    with requests.get(args.url, stream=True, timeout=(10, 90),
                      headers={"Accept": "text/event-stream"}) as r:
        r.raise_for_status()
        event = None
        for line in r.iter_lines(decode_unicode=True):
            if line == "":
                event = None
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                payload = line[5:].strip()
                if event == "error":
                    print("  [error]", payload)
                    continue
                if event != "voter":
                    continue
                try:
                    data = json.loads(decrypt(payload, key))
                except Exception as e:
                    print("  [decrypt failed]", e)
                    continue
                if args.raw:
                    print(json.dumps(data))
                    continue
                clients = data.get("clients", [])
                def nm(c):
                    return c.get("name") or c.get("client") or "?"
                def is_voted(c):
                    return (c.get("isVoted") if "isVoted" in c
                            else c.get("barcolor") == "greenyellow")
                voted = [nm(c) for c in clients
                         if is_voted(c) and int(c.get("rssi") or 0) > 0]
                rssis = " ".join(f"{nm(c)}={c.get('rssi')}" for c in clients)
                print(f"  node {data.get('node')}: {rssis}"
                      + (f"   VOTED: {', '.join(voted)}" if voted else ""))


if __name__ == "__main__":
    main()
