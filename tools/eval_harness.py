#!/usr/bin/env python3
"""Leakage-safe evaluation harness for Squelch speaker-ID + transcription.

Phase 0 of the ML uplift roadmap: the ruler that makes every later change
(WCCN backend, ReDimNet swap, LoRA, threshold tweaks) a decidable number
instead of a guess.

Design principles
-----------------
* Read-only, pure CPU. Reads SQLite + the stored per-transmission embeddings.
* Reuses the PRODUCTION scoring (`speakerid.SpeakerIdentifier.evaluate`) and
  the PRODUCTION decision (`speakerid.voice_decision`) — never a
  reimplementation, so the numbers reflect the real system.
* Leakage-safe: a trial over is NEVER scored against a profile that contains
  it, and profiles are rebuilt per-trial only from OTHER ground-truth overs,
  capped like production (`_PROFILE_CAP`).
* Honest about the flywheel's self-flattering tendency: two regimes are
  reported — `loo` (best case) and `xsession` (drops the trial speaker's
  same-session overs; the generalization number). Plus an open-set
  unknown-rejection number, because the core job on a wide-area net is
  ABSTAINING on voices never heard before.

Gold labels = transmissions with speaker_verified in ('mdc','callsign',
'manual'). MDC = radio hardware truth; manual = a human; callsign = a
confident spoken self-ID. For CallER we use only MDC/manual overs, whose
truth is INDEPENDENT of the transcript (callsign-verified overs are circular).

Usage
-----
    python tools/eval_harness.py --db /path/to/squelch.db [--days N]
        [--assign-cos 0.60 --assign-margin 0.05 --suggest-cos 0.49]
        [--config /etc/squelch/squelch.toml] [--json report.json]
        [--freeze-wer 200 --wer-out wer_set.json]   # snapshot a WER benchmark
        [--wer-refs wer_set.json]                    # score WER vs corrected refs
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from squelch.speakerid import SpeakerIdentifier, voice_decision  # noqa: E402
from squelch.callsigns import extract_callsigns  # noqa: E402

# mirror production (db.Database._PROFILE_CAP); a trial speaker needs at least
# MIN_PROFILE other overs after exclusions to count as a "known speaker" trial
PROFILE_CAP = 20
MIN_PROFILE = 2
SESSION_GAP_S = 1800.0  # 30 min: overs closer than this to the trial (same
                        # speaker) are "same session" and dropped in xsession


# --------------------------------------------------------------------------
# metric primitives
# --------------------------------------------------------------------------
def eer(target: np.ndarray, nontarget: np.ndarray) -> tuple[float, float]:
    """Equal-error-rate and the threshold at which it occurs."""
    if len(target) == 0 or len(nontarget) == 0:
        return float("nan"), float("nan")
    thr = np.unique(np.concatenate([target, nontarget]))
    # sweep descending; FRR = targets below thr, FAR = nontargets at/above thr
    best_gap, best = 1e9, (float("nan"), float("nan"))
    ts = np.sort(target)
    ns = np.sort(nontarget)
    for t in thr:
        frr = np.searchsorted(ts, t, side="left") / len(ts)
        far = (len(ns) - np.searchsorted(ns, t, side="left")) / len(ns)
        gap = abs(frr - far)
        if gap < best_gap:
            best_gap, best = gap, ((frr + far) / 2.0, float(t))
    return best


def _pav(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Pool-adjacent-violators isotonic regression (non-decreasing fit of y
    with weights w). Returns the fitted values."""
    y = y.astype(float).copy()
    w = w.astype(float).copy()
    n = len(y)
    # each block: (value, weight, start, len)
    vals = list(y)
    wts = list(w)
    lens = [1] * n
    i = 0
    stack_v: list[float] = []
    stack_w: list[float] = []
    stack_l: list[int] = []
    for j in range(n):
        v, ww, ll = vals[j], wts[j], lens[j]
        while stack_v and stack_v[-1] >= v:
            pv, pw, pl = stack_v.pop(), stack_w.pop(), stack_l.pop()
            v = (pv * pw + v * ww) / (pw + ww)
            ww = pw + ww
            ll = pl + ll
        stack_v.append(v)
        stack_w.append(ww)
        stack_l.append(ll)
    out = np.empty(n)
    idx = 0
    for v, ll in zip(stack_v, stack_l):
        out[idx:idx + ll] = v
        idx += ll
    return out


def min_cllr(target: np.ndarray, nontarget: np.ndarray) -> float:
    """min-Cllr: the cost of the log-likelihood-ratios AFTER an optimal
    monotonic recalibration (PAV). The right lens for a calibration-bound
    channel — it isolates intrinsic discrimination from threshold placement.
    0 = perfect, ~1 = useless (log2 units)."""
    nt, nn = len(target), len(nontarget)
    if nt == 0 or nn == 0:
        return float("nan")
    scores = np.concatenate([target, nontarget])
    labels = np.concatenate([np.ones(nt), np.zeros(nn)])
    order = np.argsort(scores, kind="mergesort")
    lab = labels[order]
    post = np.clip(_pav(lab, np.ones(len(lab))), 1e-6, 1 - 1e-6)
    prior = nt / (nt + nn)
    llr = np.log(post / (1 - post)) - np.log(prior / (1 - prior))
    inv = np.empty_like(llr)
    inv[order] = llr
    llr_t = inv[:nt]
    llr_n = inv[nt:]
    c_t = np.mean(np.log1p(np.exp(-llr_t)))
    c_n = np.mean(np.log1p(np.exp(llr_n)))
    return float((c_t + c_n) / (2 * np.log(2)))


def wer(ref: str, hyp: str) -> tuple[int, int]:
    """Word errors and reference length (Levenshtein over words)."""
    r = ref.lower().split()
    h = hyp.lower().split()
    if not r:
        return (len(h), 0)
    d = np.zeros((len(r) + 1, len(h) + 1), dtype=int)
    d[:, 0] = np.arange(len(r) + 1)
    d[0, :] = np.arange(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            cost = 0 if r[i - 1] == h[j - 1] else 1
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + cost)
    return int(d[len(r), len(h)]), len(r)


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
class Over:
    __slots__ = ("id", "at", "spk", "verified", "emb", "transcript", "label")

    def __init__(self, row, emb):
        self.id = row["id"]
        self.at = row["started_at"]
        self.spk = row["speaker_id"]
        self.verified = row["speaker_verified"]
        self.emb = emb
        self.transcript = row["transcript"] or ""
        self.label = row["label"] or ""


def load_gold(db_path: str, dim: int):
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT t.id, t.started_at, t.speaker_id, t.speaker_verified,"
        "       t.embedding, t.transcript, s.label"
        " FROM transmissions t JOIN speakers s ON s.id = t.speaker_id"
        " WHERE t.speaker_verified IN ('mdc','callsign','manual')"
        "   AND t.embedding IS NOT NULL"
        " ORDER BY t.id").fetchall()
    con.close()
    overs, skipped = [], 0
    for r in rows:
        e = np.frombuffer(r["embedding"], dtype=np.float32)
        if len(e) != dim:
            skipped += 1
            continue
        overs.append(Over(r, e))
    return overs, skipped


def build_profiles(trial: Over, by_spk: dict, xsession: bool):
    """Leakage-safe per-speaker profiles for one trial: exclude the trial
    itself always; in xsession also drop the trial speaker's same-session
    overs. Capped at PROFILE_CAP most-recent (mirrors production)."""
    profs = []
    for spk, overs in by_spk.items():
        pool = [o for o in overs if o.id != trial.id]
        if xsession and spk == trial.spk:
            pool = [o for o in pool if abs(o.at - trial.at) > SESSION_GAP_S]
        if not pool:
            continue
        pool.sort(key=lambda o: o.id, reverse=True)
        mat = np.stack([o.emb for o in pool[:PROFILE_CAP]])
        profs.append((spk, mat))
    return profs


# --------------------------------------------------------------------------
# speaker-ID evaluation
# --------------------------------------------------------------------------
class _StubDB:
    def __init__(self):
        self.profiles = []

    def load_speaker_profiles(self, dim):
        return self.profiles


def eval_speaker(overs, thr, xsession: bool):
    by_spk: dict[int, list] = {}
    for o in overs:
        by_spk.setdefault(o.spk, []).append(o)

    sid = SpeakerIdentifier(_StubDB(), 0.0, False, backend="titanet")
    a_cos, a_mgn, s_cos = thr

    tgt, imp = [], []           # verification scores
    known = 0                   # trials whose own speaker has a profile
    conf = {"assign_ok": 0, "assign_wrong": 0, "suggest_ok": 0,
            "suggest_wrong": 0, "abstain": 0}
    openset = {"assign": 0, "suggest": 0, "abstain": 0, "n": 0}
    rows = []                   # per-trial (margin, correct-assign?) for strata

    for t in overs:
        profs = build_profiles(t, by_spk, xsession)
        sid.db.profiles = profs
        ev = sid.evaluate(t.emb)
        raw = ev["raw"]
        has_self = t.spk in raw

        # --- verification trials (target vs each imposter) ---
        if has_self:
            tgt.append(raw[t.spk])
        for k, v in raw.items():
            if k != t.spk:
                imp.append(v)

        # --- operational decision at live thresholds (known speakers) ---
        if has_self:
            known += 1
            dec, best, cos = voice_decision(ev, a_cos, a_mgn, s_cos)
            ok = best == t.spk
            if dec == "assign":
                conf["assign_ok" if ok else "assign_wrong"] += 1
            elif dec == "suggest":
                conf["suggest_ok" if ok else "suggest_wrong"] += 1
            else:
                conf["abstain"] += 1
            rows.append((ev["margin"], dec, ok))

        # --- open-set: pretend this speaker is UNKNOWN (drop it), must abstain ---
        others = {k: v for k, v in raw.items() if k != t.spk}
        if others:
            openset["n"] += 1
            ordr = sorted(others.items(), key=lambda kv: kv[1], reverse=True)
            b_cos = ordr[0][1]
            second = ordr[1][1] if len(ordr) > 1 else -1.0
            ev_u = {"best_id": ordr[0][0], "best_cos": b_cos,
                    "second_cos": second, "margin": b_cos - second, "raw": others}
            dec_u, _, _ = voice_decision(ev_u, a_cos, a_mgn, s_cos)
            openset["assign" if dec_u == "assign"
                    else "suggest" if dec_u == "suggest" else "abstain"] += 1

    tgt = np.array(tgt)
    imp = np.array(imp)
    e, e_thr = eer(tgt, imp)
    res = {
        "regime": "cross-session" if xsession else "leave-one-out",
        "n_trials": len(overs),
        "n_known_trials": known,
        "n_target": len(tgt),
        "n_imposter": len(imp),
        "eer": e,
        "eer_threshold": e_thr,
        "min_cllr": min_cllr(tgt, imp),
        "target_cos_median": float(np.median(tgt)) if len(tgt) else float("nan"),
        "imposter_cos_median": float(np.median(imp)) if len(imp) else float("nan"),
        "operational": _operational(conf, known),
        "open_set_unknown": _openset(openset),
        "hard_stratum": _hard(rows),
        "accuracy_vs_coverage": _sweep(overs, by_spk, xsession, a_mgn, s_cos),
    }
    return res


def _operational(conf, known):
    if not known:
        return {"note": "no known-speaker trials"}
    assigned = conf["assign_ok"] + conf["assign_wrong"]
    return {
        **conf,
        "coverage_assigned": assigned / known,
        "assign_precision": (conf["assign_ok"] / assigned) if assigned else float("nan"),
        "wrong_name_rate": conf["assign_wrong"] / known,
    }


def _openset(o):
    if not o["n"]:
        return {"note": "no open-set trials"}
    return {
        "n": o["n"],
        "false_assign_rate": o["assign"] / o["n"],
        "false_suggest_rate": o["suggest"] / o["n"],
        "correct_abstain_rate": o["abstain"] / o["n"],
    }


def _hard(rows):
    """Operational picture on the low-margin (near-neighbour) third — the
    population that actually causes wrong names / 'unknown voice'."""
    if len(rows) < 6:
        return {"note": "too few trials to stratify"}
    margins = np.array([r[0] for r in rows])
    cut = np.percentile(margins, 33)
    hard = [r for r in rows if r[0] <= cut]
    assigned = sum(1 for _, d, _ in hard if d == "assign")
    aw = sum(1 for _, d, ok in hard if d == "assign" and not ok)
    return {
        "margin_cut": float(cut),
        "n": len(hard),
        "coverage_assigned": assigned / len(hard) if hard else 0.0,
        "assign_precision": (1 - aw / assigned) if assigned else float("nan"),
        "wrong_name_rate": aw / len(hard) if hard else 0.0,
    }


def _sweep(overs, by_spk, xsession, a_mgn, s_cos):
    """Coverage vs precision as assign_cos sweeps — the tradeoff curve. Reuses
    the same evaluate() outputs (recomputed per threshold is cheap)."""
    sid = SpeakerIdentifier(_StubDB(), 0.0, False, backend="titanet")
    evs = []
    for t in overs:
        sid.db.profiles = build_profiles(t, by_spk, xsession)
        ev = sid.evaluate(t.emb)
        if t.spk in ev["raw"]:
            evs.append((ev, t.spk))
    out = []
    for a_cos in [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]:
        ok = wrong = 0
        for ev, spk in evs:
            dec, best, _ = voice_decision(ev, a_cos, a_mgn, s_cos)
            if dec == "assign":
                ok += best == spk
                wrong += best != spk
        assigned = ok + wrong
        out.append({
            "assign_cos": a_cos,
            "coverage": assigned / len(evs) if evs else 0.0,
            "precision": ok / assigned if assigned else float("nan"),
        })
    return out


# --------------------------------------------------------------------------
# transcription: CallER (automatic) + WER (against frozen refs)
# --------------------------------------------------------------------------
def eval_caller(overs):
    """On MDC/manual overs (truth independent of the transcript), did the
    transcript recover the operator's callsign? callsign-verified overs are
    reported separately as a circular sanity check."""
    def call_of(label):
        cs = extract_callsigns(label)
        return cs[0] if cs else None

    buckets = {"mdc_manual": [0, 0], "callsign": [0, 0]}
    for o in overs:
        truth = call_of(o.label)
        if not truth or not o.transcript.strip():
            continue
        hit = truth in extract_callsigns(o.transcript)
        key = "callsign" if o.verified == "callsign" else "mdc_manual"
        buckets[key][0] += hit
        buckets[key][1] += 1
    out = {}
    for k, (hit, n) in buckets.items():
        out[k] = {"n": n, "caller": (1 - hit / n) if n else float("nan"),
                  "recall": (hit / n) if n else float("nan")}
    return out


def freeze_wer(db_path, n, out_path):
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, transcript FROM transmissions"
        " WHERE speaker_verified IN ('mdc','callsign','manual')"
        "   AND transcript IS NOT NULL AND transcript != ''"
        " ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    con.close()
    data = [{"id": r["id"], "machine": r["transcript"], "reference": ""}
            for r in rows]
    Path(out_path).write_text(json.dumps(data, indent=1))
    print(f"wrote {len(data)} overs to {out_path} — fill in 'reference' for each "
          f"(correct the transcript by ear), then re-run with --wer-refs {out_path}")


def eval_wer(db_path, refs_path):
    refs = json.loads(Path(refs_path).read_text())
    refs = [r for r in refs if (r.get("reference") or "").strip()]
    if not refs:
        return {"note": "no corrected references yet"}
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    err = length = 0
    for r in refs:
        row = con.execute("SELECT transcript FROM transmissions WHERE id=?",
                          (r["id"],)).fetchone()
        hyp = (row["transcript"] if row else "") or ""
        e, ln = wer(r["reference"], hyp)
        err += e
        length += ln
    con.close()
    return {"n": len(refs), "wer": err / length if length else float("nan"),
            "words": length}


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--dim", type=int, default=192)
    ap.add_argument("--days", type=float, default=0,
                    help="restrict TRIALS to the last N days (0 = all gold)")
    ap.add_argument("--config", help="squelch.toml to read thresholds from")
    ap.add_argument("--assign-cos", type=float, default=0.60)
    ap.add_argument("--assign-margin", type=float, default=0.05)
    ap.add_argument("--suggest-cos", type=float, default=0.49)
    ap.add_argument("--json", help="write the full metrics snapshot here")
    ap.add_argument("--freeze-wer", type=int, help="snapshot N overs for WER labeling")
    ap.add_argument("--wer-out", default="wer_set.json")
    ap.add_argument("--wer-refs", help="score WER against a corrected refs file")
    args = ap.parse_args()

    if args.freeze_wer:
        freeze_wer(args.db, args.freeze_wer, args.wer_out)
        return

    if args.config:
        from squelch.config import load_config
        cfg = load_config(args.config)
        thr = (cfg.assign_cos, cfg.assign_margin, cfg.suggest_cos)
    else:
        thr = (args.assign_cos, args.assign_margin, args.suggest_cos)

    overs, skipped = load_gold(args.db, args.dim)
    trials = overs
    if args.days > 0:
        cutoff = time.time() - args.days * 86400
        trials = [o for o in overs if o.at >= cutoff]

    n_spk = len({o.spk for o in overs})
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db": args.db,
        "thresholds": {"assign_cos": thr[0], "assign_margin": thr[1],
                       "suggest_cos": thr[2]},
        "gold_overs": len(overs),
        "gold_speakers": n_spk,
        "trial_overs": len(trials),
        "wrong_dim_skipped": skipped,
        "speaker_id": {
            "leave_one_out": eval_speaker(overs, thr, xsession=False),
            "cross_session": eval_speaker(overs, thr, xsession=True),
        },
        "transcription": {"caller": eval_caller(overs)},
    }
    if args.wer_refs:
        report["transcription"]["wer"] = eval_wer(args.db, args.wer_refs)

    _print(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1))
        print(f"\nsnapshot -> {args.json}")


def _print(r):
    p = print
    p("=" * 68)
    p(f"Squelch eval - {r['generated_at']}")
    p(f"gold overs: {r['gold_overs']}  speakers: {r['gold_speakers']}  "
      f"(skipped wrong-dim: {r['wrong_dim_skipped']})")
    t = r["thresholds"]
    p(f"thresholds: assign>={t['assign_cos']} margin>={t['assign_margin']} "
      f"suggest>={t['suggest_cos']}")
    for regime in ("leave_one_out", "cross_session"):
        s = r["speaker_id"][regime]
        p("-" * 68)
        p(f"SPEAKER ID [{s['regime']}]  target={s['n_target']} "
          f"imposter={s['n_imposter']}")
        p(f"  EER              {s['eer']:.4f}   (min-Cllr {s['min_cllr']:.4f})")
        p(f"  cos medians      target {s['target_cos_median']:.3f} / "
          f"imposter {s['imposter_cos_median']:.3f}")
        o = s["operational"]
        if "note" not in o:
            p(f"  @live thresholds  coverage {o['coverage_assigned']:.1%}  "
              f"assign-precision {o['assign_precision']:.1%}  "
              f"WRONG-NAME {o['wrong_name_rate']:.2%}")
            p(f"                    ({o['assign_ok']} ok / {o['assign_wrong']} wrong "
              f"assigns, {o['suggest_ok']}/{o['suggest_wrong']} suggests, "
              f"{o['abstain']} abstain)")
        u = s["open_set_unknown"]
        if "note" not in u:
            p(f"  open-set unknown  false-assign {u['false_assign_rate']:.2%}  "
              f"false-suggest {u['false_suggest_rate']:.2%}  "
              f"correct-abstain {u['correct_abstain_rate']:.1%}  (n={u['n']})")
        h = s["hard_stratum"]
        if "note" not in h:
            p(f"  hard (low-margin) coverage {h['coverage_assigned']:.1%}  "
              f"assign-precision {h['assign_precision']:.1%}  "
              f"WRONG-NAME {h['wrong_name_rate']:.2%}  (n={h['n']})")
        p("  accuracy vs coverage (assign_cos sweep):")
        for pt in s["accuracy_vs_coverage"]:
            pr = pt["precision"]
            p(f"      cos>={pt['assign_cos']:.2f}  cov {pt['coverage']:5.1%}  "
              f"prec {pr:.1%}" if pr == pr else
              f"      cos>={pt['assign_cos']:.2f}  cov {pt['coverage']:5.1%}  prec  n/a")
    p("-" * 68)
    c = r["transcription"]["caller"]
    mm, cc = c["mdc_manual"], c["callsign"]
    p(f"TRANSCRIPTION CallER (MDC/manual, transcript-independent): "
      f"{mm['caller']:.1%} miss  (recall {mm['recall']:.1%}, n={mm['n']})")
    p(f"  callsign-verified (circular sanity): {cc['caller']:.1%} miss (n={cc['n']})")
    if "wer" in r["transcription"]:
        w = r["transcription"]["wer"]
        p(f"  WER: {w.get('wer', float('nan')):.1%} (n={w.get('n', 0)})"
          if "note" not in w else f"  WER: {w['note']}")
    p("=" * 68)


if __name__ == "__main__":
    main()
