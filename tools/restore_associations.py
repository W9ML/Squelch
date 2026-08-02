#!/usr/bin/env python3
"""Restore pre-reset speaker associations from a backup, for EMPTY speakers.

Recovery step 2. The 2026-07-19 voiceprint reset orphaned voice-carried
regulars (set speaker_id=NULL on their overs), leaving named operators with no
voiceprint. This reads a pre-reset backup DB, and for each currently-EMPTY
named speaker, finds which of TODAY's transmissions were attributed to that
same operator (matched by CALLSIGN across the two DBs) back then. It restores
those attributions on the overs that are still unassigned and enrolls their
CURRENT embeddings — giving the quiet regulars a voiceprint again.

Scoped deliberately: only speakers that currently have ZERO enrolled samples
(nothing to pollute), and only overs that are currently unassigned (never
overrides a live decision). The backup's attributions were made by the old
engine (mostly voice), so a few may be imperfect — that's the accepted cost of
recovering coverage. Dry-run by default; --apply to write.

    python tools/restore_associations.py --db CURRENT.db --backup PRERESET.db [--apply]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from squelch.db import Database  # noqa: E402
from squelch.callsigns import extract_callsigns  # noqa: E402


def _call(label: str) -> str | None:
    cs = extract_callsigns(label or "")
    return cs[0] if cs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="current (live) DB")
    ap.add_argument("--backup", required=True, help="pre-reset backup DB")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-dur", type=int, default=1500)
    args = ap.parse_args()

    cur = Database(args.db)
    bak = sqlite3.connect(f"file:{Path(args.backup).as_posix()}?mode=ro", uri=True)
    bak.row_factory = sqlite3.Row

    # backup: callsign -> set(tx_ids) that were attributed to that operator
    bak_by_call: dict[str, dict[int, str]] = {}
    for r in bak.execute(
            "SELECT t.id, t.speaker_verified v, s.label FROM transmissions t"
            " JOIN speakers s ON s.id = t.speaker_id"
            " WHERE t.speaker_id IS NOT NULL AND s.is_named = 1"):
        c = _call(r["label"])
        if c:
            bak_by_call.setdefault(c, {})[r["id"]] = r["v"] or "voice"
    bak.close()

    # current empty named speakers, keyed by their callsign
    empties = cur._conn.execute(
        "SELECT s.id, s.label FROM speakers s WHERE s.is_named = 1"
        " AND NOT EXISTS (SELECT 1 FROM speaker_embeddings e"
        "                 WHERE e.speaker_id = s.id)").fetchall()

    plan = []   # (speaker_id, label, [(tx_id, verified)])
    for s in empties:
        c = _call(s["label"])
        if not c or c not in bak_by_call:
            continue
        want = bak_by_call[c]
        # keep only overs that STILL exist, are currently unassigned, have an emb
        rows = cur._conn.execute(
            "SELECT id, embedding, duration_ms FROM transmissions"
            " WHERE speaker_id IS NULL AND embedding IS NOT NULL"
            f"   AND id IN ({','.join(str(i) for i in want)})").fetchall()
        if rows:
            plan.append((s["id"], s["label"],
                         [(r["id"], want[r["id"]], r["embedding"],
                           r["duration_ms"] or 0) for r in rows]))

    total = sum(len(p[2]) for p in plan)
    print(f"empty named speakers: {len(empties)}  "
          f"matched in backup with restorable overs: {len(plan)}")
    print(f"overs to restore: {total}\n")
    for spk, label, overs in sorted(plan, key=lambda p: -len(p[2])):
        enr = sum(1 for _, _, _, dur in overs if dur >= args.min_dur)
        print(f"  {label:14} restore {len(overs):3} overs  (enroll ~{min(20, enr)})")

    if not args.apply:
        print("\n(dry run - rerun with --apply to write)")
        return

    reatt = enrolled = 0
    for spk, label, overs in plan:
        for tx_id, verified, blob, dur in overs:
            cur.assign_speaker(tx_id, spk, None, verified)
            reatt += 1
            if dur >= args.min_dur:
                emb = np.frombuffer(blob, dtype=np.float32)
                if cur.add_speaker_embedding(spk, emb, tx_id=tx_id,
                                             verified=(verified in ("mdc", "manual", "callsign"))):
                    enrolled += 1
    n = cur._conn.execute(
        "SELECT COUNT(DISTINCT speaker_id) FROM speaker_embeddings").fetchone()[0]
    print(f"\napplied: {reatt} overs restored, {enrolled} embeddings enrolled; "
          f"{n} speakers now have a profile")


if __name__ == "__main__":
    main()
