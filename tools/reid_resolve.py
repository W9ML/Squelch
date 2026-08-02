#!/usr/bin/env python3
"""Re-run full identity resolution on recent overs where a callsign was heard.

`reid_recent.py` re-scores voice only. This one re-runs the WHOLE pipeline
decision (MDC > callsign > voice) on recent overs that named a callsign but
never got a name — so the callsign-ordering fix (a spoken call outranks a
coin-flip voice guess of someone else) is applied retroactively.

Scope: overs in the window that heard a speaker callsign and are still either
Unknown, a bare suggestion, or in an auto-cluster (never a manual / MDC /
callsign-verified name, never an already-named voice match). Auto-clustering
is turned OFF for the pass so an unmatched over stays Unknown instead of
spawning a new ghost cluster.

    reid_resolve.py -c /etc/squelch/squelch.toml --hours 24            # dry run
    reid_resolve.py -c /etc/squelch/squelch.toml --hours 24 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from squelch.callsigns import speaker_callsign
from squelch.config import load_config
from squelch.db import Database
from squelch.pipeline import Pipeline


class _NullBroadcaster:
    async def send(self, *a, **k):
        pass

    def send_soon(self, *a, **k):
        pass


async def _amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", default="/etc/squelch/squelch.toml")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--apply", action="store_true",
                    help="write the re-resolved assignments (default dry run)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg.autocluster = False              # never spawn ghosts during a re-resolve
    db = Database(cfg.db_path)
    pipe = Pipeline(cfg, db, _NullBroadcaster())
    cut = time.time() - args.hours * 3600
    labels = {s["id"]: s["label"] for s in db.list_speakers()}

    rows = db._conn.execute(
        "SELECT t.id, t.transcript FROM transmissions t"
        " LEFT JOIN speakers s ON s.id = t.speaker_id"
        " WHERE t.started_at > ? AND t.embedding IS NOT NULL"
        " AND t.transcript IS NOT NULL"
        " AND (t.speaker_verified IS NULL OR t.speaker_verified = 'voice')"
        " AND (t.speaker_id IS NULL OR s.is_named = 0)"
        " ORDER BY t.started_at", (cut,)).fetchall()

    def _state(tx):
        r = db.get_transmission(tx)
        return (labels.get(r["speaker_id"], "unknown" if r["speaker_id"] is None
                           else r["speaker_label"]),
                labels.get(r["suggest_speaker_id"]))

    changed = 0
    for r in rows:
        cs, _ = speaker_callsign(r["transcript"] or "")
        if not cs:                       # no speaker callsign heard -> skip
            continue
        emb = db.get_tx_embedding(r["id"])
        if emb is None:
            continue
        ev = pipe.speaker_id.evaluate(emb)
        before = _state(r["id"])
        if not args.apply:
            # dry run: show what the (weak/strong) callsign would resolve to
            owner = db.find_speaker_by_callsign(cs)
            print(f"  tx {r['id']} ({cs!r} in {r['transcript'][:32]!r}): "
                  f"now {before[0]}/sugg={before[1]} -> owner "
                  f"{labels.get(owner, 'unknown')} voice={ev['raw'].get(owner)}")
            continue
        # relabel the freshly-updated speaker map after each write
        labels.update({s["id"]: s["label"] for s in db.list_speakers()})
        await pipe._resolve_identity(r["id"], emb, ev)
        after = _state(r["id"])
        if after != before:
            changed += 1
            print(f"  tx {r['id']} {cs}: {before[0]}/sugg={before[1]} "
                  f"-> {after[0]}/sugg={after[1]}")

    print(f"{'APPLIED' if args.apply else 'DRY RUN'}: "
          f"{changed} overs re-resolved (of {len(rows)} candidates scanned)")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
