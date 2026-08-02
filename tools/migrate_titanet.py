#!/usr/bin/env python3
"""One-time migration: resemblyzer (256-d) -> TitaNet (192-d) voiceprints.

Run on the deployment host with the service STOPPED and the ONNX exported:

    systemctl stop squelch
    /opt/squelch/venv/bin/python tools/migrate_titanet.py \
        -c /etc/squelch/squelch.toml [--apply]
    # set [speaker_id] embedder = "titanet" in the config
    systemctl start squelch

Without --apply it only re-embeds into memory and prints the calibration
report (same/cross-speaker cosine distributions + suggested thresholds).
With --apply it also:
  * rewrites transmissions.embedding for every tx whose audio is retained
  * wipes speaker_embeddings and re-enrolls per speaker from their retained,
    strongest-provenance transmissions (manual/mdc/callsign first), capped
  * clears legacy speakers.centroid blobs

Speakers whose audio has all been pruned keep their label but lose their
voiceprint (they re-learn from their next transmission, exactly like the
2026-07-03 resemblyzer reset)."""

import argparse
import sys
import time
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from squelch.config import load_config            # noqa: E402
from squelch.db import Database                   # noqa: E402
from squelch.pipeline import Pipeline             # noqa: E402
from squelch.titanet import TitaNetEmbedder       # noqa: E402

ENROLL_CAP = 40
# provenance ranking for enrollment: hardware/manual truth first
_RANK = {"manual": 0, "mdc": 1, "callsign": 2}


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = Database(cfg.db_path)
    embedder = TitaNetEmbedder(cfg.data_dir / "models" / "titanet_large.onnx")

    rows = db._conn.execute(
        "SELECT id, audio_path, duration_ms, speaker_id, speaker_verified"
        " FROM transmissions WHERE audio_path IS NOT NULL").fetchall()
    print(f"{len(rows)} transmissions with retained audio")

    embs: dict[int, np.ndarray] = {}
    t0 = time.time()
    skipped = 0
    for r in rows:
        p = Path(r["audio_path"])
        if not p.is_absolute():
            p = cfg.data_dir / p
        if not p.exists() or (r["duration_ms"] or 0) < cfg.min_embed_ms:
            skipped += 1
            continue
        pcm = read_wav(p)
        a16 = Pipeline._to_16k_float(pcm)
        speech = Pipeline._speech_only(a16)
        if speech is None:
            skipped += 1
            continue
        e = embedder.embed(speech)
        embs[r["id"]] = e / (np.linalg.norm(e) or 1e-9)
    print(f"embedded {len(embs)} ({skipped} skipped: short/pruned/no speech) "
          f"in {time.time() - t0:.1f}s")

    # ---- calibration report on named speakers ----
    by_speaker: dict[int, list[int]] = defaultdict(list)
    prov: dict[int, str] = {}
    for r in rows:
        if r["id"] in embs and r["speaker_id"] is not None:
            by_speaker[r["speaker_id"]].append(r["id"])
            prov[r["id"]] = r["speaker_verified"] or ""
    named = {}
    for spk_id, tx_ids in by_speaker.items():
        s = db.get_speaker(spk_id)
        if s and s["is_named"] and len(tx_ids) >= 2:
            named[spk_id] = (s["label"], tx_ids)

    same, cross = [], []
    ids = list(named)
    for i, a in enumerate(ids):
        va = np.stack([embs[t] for t in named[a][1]])
        sims = va @ va.T
        iu = np.triu_indices(len(va), k=1)
        same.extend(sims[iu].tolist())
        for b in ids[i + 1:]:
            vb = np.stack([embs[t] for t in named[b][1]])
            cross.extend((va @ vb.T).ravel().tolist())

    if same and cross:
        s, c = np.array(same), np.array(cross)
        print(f"\nnamed speakers with >=2 retained txs: {len(named)}")
        print(f"same-speaker cosine:  p5={np.percentile(s, 5):.3f} "
              f"median={np.median(s):.3f} p95={np.percentile(s, 95):.3f}")
        print(f"cross-speaker cosine: p5={np.percentile(c, 5):.3f} "
              f"median={np.median(c):.3f} p95={np.percentile(c, 95):.3f}")
        # assign: above nearly all cross pairs; suggest: a step below
        assign = round(float(max(np.percentile(c, 99), np.percentile(s, 25))), 2)
        suggest = round(float(np.percentile(c, 95)), 2)
        print(f"suggested [speaker_id] assign_cos = {assign}, "
              f"suggest_cos = {suggest} (review before applying)")
    else:
        print("not enough named-speaker data for calibration")

    if not args.apply:
        print("\n(dry run — rerun with --apply to write)")
        return

    # ---- apply ----
    with db._lock, db._conn:
        for tx_id, e in embs.items():
            db._conn.execute("UPDATE transmissions SET embedding=? WHERE id=?",
                             (e.astype(np.float32).tobytes(), tx_id))
        db._conn.execute("DELETE FROM speaker_embeddings")
        db._conn.execute("UPDATE speakers SET centroid=NULL, n_samples=0")
        enrolled = 0
        for spk_id, tx_ids in by_speaker.items():
            ranked = sorted(tx_ids,
                            key=lambda t: (_RANK.get(prov[t], 3), -t))
            for t in ranked[:ENROLL_CAP]:
                db._conn.execute(
                    "INSERT INTO speaker_embeddings"
                    " (speaker_id, tx_id, embedding, verified, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (spk_id, t, embs[t].astype(np.float32).tobytes(),
                     1 if prov[t] in _RANK else 0, time.time()))
                enrolled += 1
        n_spk = db._conn.execute(
            "SELECT COUNT(DISTINCT speaker_id) FROM speaker_embeddings"
        ).fetchone()[0]
    print(f"\napplied: {len(embs)} tx embeddings rewritten, {enrolled} "
          f"samples enrolled across {n_spk} speakers")
    print('now set [speaker_id] embedder = "titanet" and start the service')


if __name__ == "__main__":
    main()
