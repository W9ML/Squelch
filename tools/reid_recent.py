#!/usr/bin/env python3
"""Re-identify recent transmissions that never got a name.

After a profile rebuild or a threshold change, overs that were left Unknown,
shown only as a "possible" suggestion, or dumped into a throwaway auto-cluster
can often be matched now. This re-scores those (and ONLY those — it never
touches manual / callsign / MDC assignments, and never reassigns an over that
already has a real name) against the current NAMED profiles, and assigns the
ones that clear the same cosine + margin bar the live pipeline uses.

    /opt/squelch/venv/bin/python tools/reid_recent.py -c /etc/squelch/squelch.toml --hours 24            # dry run
    /opt/squelch/venv/bin/python tools/reid_recent.py -c /etc/squelch/squelch.toml --hours 24 --apply    # write

It only reads profiles; it does not enroll (no feedback pollution). A later
Rebuild will pick up the freshly-assigned overs on its own.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from squelch.config import load_config
from squelch.db import Database
from squelch.pipeline import Pipeline
from squelch.speakerid import _top_k_mean


class _NullBroadcaster:
    async def send(self, *a, **k):
        pass

    def send_soon(self, *a, **k):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", default="/etc/squelch/squelch.toml")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--apply", action="store_true",
                    help="write the assignments (default is a dry run)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    pipe = Pipeline(cfg, db, _NullBroadcaster())
    dim = pipe.speaker_id.embedding_dim
    cut = time.time() - args.hours * 3600
    write = args.apply

    speakers = db.list_speakers()
    labels = {s["id"]: s["label"] for s in speakers}
    named_ids = {s["id"] for s in speakers if s["is_named"]}
    profiles = [(spk, mat) for spk, mat in db.load_speaker_profiles(dim)
                if spk in named_ids]
    if not profiles:
        print("no named profiles to match against"); return 0
    print(f"matching against {len(profiles)} named profiles, "
          f"assign_cos={cfg.assign_cos} margin={cfg.assign_margin} "
          f"suggest_cos={cfg.suggest_cos}{'' if write else '  [DRY RUN]'}")

    # candidates: recent overs with a voice embedding that are NOT identified
    # by a name — Unknown (NULL), a suggestion, or in an auto-cluster
    rows = db._conn.execute(
        "SELECT t.id, t.embedding, t.speaker_id, s.is_named FROM transmissions t"
        " LEFT JOIN speakers s ON s.id = t.speaker_id"
        " WHERE t.started_at > ? AND t.embedding IS NOT NULL"
        " AND (t.speaker_id IS NULL OR s.is_named = 0)"
        " ORDER BY t.started_at", (cut,)).fetchall()

    assigned = suggested = 0
    for r in rows:
        e = np.frombuffer(r["embedding"], dtype=np.float32)
        if len(e) != dim:
            continue
        e = e / (np.linalg.norm(e) or 1.0)
        raw = {}
        for spk, mat in profiles:
            norms = np.linalg.norm(mat, axis=1)
            norms[norms == 0] = 1e-9
            raw[spk] = _top_k_mean((mat / norms[:, None]) @ e)
        order = sorted(raw.items(), key=lambda kv: kv[1], reverse=True)
        best, bc = order[0]
        margin = bc - (order[1][1] if len(order) > 1 else -1.0)
        cur = labels.get(r["speaker_id"], "unknown")
        if bc >= cfg.assign_cos and margin >= cfg.assign_margin:
            print(f"  tx {r['id']}: {cur} -> {labels[best]} (cos {bc:.2f}, margin {margin:.2f})")
            if write:
                db.assign_speaker(r["id"], best, bc, "voice")
            assigned += 1
        elif r["speaker_id"] is None and bc >= cfg.suggest_cos:
            if write:
                db.set_suggestion(r["id"], best, bc)
            suggested += 1

    # any auto-clusters emptied by reassignment are pruned
    pruned = 0
    if write:
        for s in speakers:
            if s["is_named"]:
                continue
            n = db._conn.execute(
                "SELECT COUNT(*) n FROM transmissions WHERE speaker_id=?",
                (s["id"],)).fetchone()["n"]
            if n == 0:
                db._conn.execute("DELETE FROM speaker_embeddings WHERE speaker_id=?", (s["id"],))
                db._conn.execute("DELETE FROM speakers WHERE id=?", (s["id"],))
                pruned += 1
        db._conn.commit()

    print(f"{'APPLIED' if write else 'DRY RUN'}: {assigned} overs named, "
          f"{suggested} new suggestions, {pruned} empty clusters pruned "
          f"(of {len(rows)} unidentified candidates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
