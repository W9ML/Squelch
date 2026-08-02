#!/usr/bin/env python3
"""Rebuild speaker voiceprints from spoken callsign self-IDs across history.

Recovery tool. The 2026-07-19 voiceprint reset set speaker_id=NULL on every
voice-attributed over, orphaning identities carried by voice (most regulars)
and leaving them with empty profiles — which then fragment into new
"Speaker N" clusters. This re-mines the STRONG spoken self-IDs still sitting
in those overs' transcripts, re-attributes each over to its operator, and
enrolls its stored embedding — rebuilding profiles from trustworthy evidence
(a spoken self-ID), never from voice-match guessing.

Only STRONG self-IDs ("this is X", phonetic spelling, FCC sign-off) that
match an EXISTING named speaker are used — high precision, no new clusters.

    python tools/rebuild_profiles.py --db /path/to/squelch.db          # dry run
    python tools/rebuild_profiles.py --db /path/to/squelch.db --apply   # write
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from squelch.db import Database  # noqa: E402
from squelch.callsigns import speaker_callsign  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-dur", type=int, default=1500,
                    help="only enroll embeddings from overs at least this long (ms)")
    args = ap.parse_args()

    db = Database(args.db)
    rows = db._conn.execute(
        "SELECT id, transcript, embedding, duration_ms FROM transmissions"
        " WHERE speaker_id IS NULL AND embedding IS NOT NULL"
        "   AND transcript IS NOT NULL AND transcript != ''"
        " ORDER BY id").fetchall()

    plan: dict[int, list] = {}          # speaker_id -> [(tx_id, blob, dur)]
    orphan_calls: dict[str, int] = {}   # strong self-ID with no existing speaker
    scanned = strong = 0
    for r in rows:
        scanned += 1
        cs, strength = speaker_callsign(r["transcript"])
        if strength != "strong":
            continue
        strong += 1
        owner = db.find_speaker_by_callsign(cs)
        if owner is None:
            orphan_calls[cs] = orphan_calls.get(cs, 0) + 1
            continue
        plan.setdefault(owner, []).append(
            (r["id"], r["embedding"], r["duration_ms"] or 0))

    # ---- report ----
    n_over = sum(len(v) for v in plan.values())
    print(f"scanned {scanned} unassigned overs with transcript+embedding")
    print(f"  strong self-IDs found: {strong}")
    print(f"  -> re-attributable to {len(plan)} existing named speakers, "
          f"{n_over} overs")
    if orphan_calls:
        top = sorted(orphan_calls.items(), key=lambda kv: -kv[1])[:8]
        print(f"  strong self-IDs with NO existing speaker (not touched): "
              f"{sum(orphan_calls.values())} overs across {len(orphan_calls)} "
              f"callsigns, e.g. {top}")

    # who recovers, and from-empty highlights
    print("\ntop speakers by overs recovered (label: +overs, profile now->after):")
    ranked = sorted(plan.items(), key=lambda kv: -len(kv[1]))
    recovered_empty = 0
    for spk, overs in ranked[:20]:
        s = db.get_speaker(spk)
        cur = db._conn.execute(
            "SELECT COUNT(*) FROM speaker_embeddings WHERE speaker_id=?",
            (spk,)).fetchone()[0]
        enroll_n = sum(1 for _, _, dur in overs if dur >= args.min_dur)
        after = min(20, cur + enroll_n)  # PROFILE_CAP
        if cur == 0:
            recovered_empty += 1
        print(f"  {(s['label'] if s else spk):14} +{len(overs):3} overs   "
              f"profile {cur:2} -> ~{after:2}")
    print(f"\n{recovered_empty} previously-EMPTY named speakers get a voiceprint")

    if not args.apply:
        print("\n(dry run — rerun with --apply to write)")
        return

    reatt = enrolled = 0
    for spk, overs in plan.items():
        for tx_id, blob, dur in overs:
            db.assign_speaker(tx_id, spk, None, "callsign")
            reatt += 1
            if dur >= args.min_dur:
                emb = np.frombuffer(blob, dtype=np.float32)
                if db.add_speaker_embedding(spk, emb, tx_id=tx_id, verified=True):
                    enrolled += 1
    n_spk = db._conn.execute(
        "SELECT COUNT(DISTINCT speaker_id) FROM speaker_embeddings").fetchone()[0]
    print(f"\napplied: {reatt} overs re-attributed, {enrolled} embeddings "
          f"enrolled; {n_spk} speakers now have a profile")


if __name__ == "__main__":
    main()
