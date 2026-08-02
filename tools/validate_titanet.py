#!/usr/bin/env python3
"""Validate the app's TitaNet runtime against NeMo ground truth.

    python tools/validate_titanet.py --model /var/lib/squelch/models/titanet_large.onnx \
        --truth /var/lib/squelch/models/titanet_truth.json

Embeds each wav from the truth file through squelch.titanet (numpy featurizer +
onnxruntime) and reports the cosine to the NeMo-computed embedding. Passes
when every cosine >= 0.995 — below that the featurizer has drifted from
NeMo's preprocessing and MUST NOT ship.
"""

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from squelch.titanet import SAMPLE_RATE, TitaNetEmbedder  # noqa: E402


def read_wav_16k(path: str) -> np.ndarray:
    with wave.open(path) as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    x = pcm.astype(np.float32) / 32768.0
    if sr == SAMPLE_RATE:
        return x
    if sr == 8000:
        # the pipeline's exact 8k->16k path (Pipeline._to_16k_float)
        from scipy.signal import resample_poly
        return resample_poly(x, 2, 1).astype(np.float32)
    raise SystemExit(f"{path}: unsupported sample rate {sr}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--truth", required=True)
    args = ap.parse_args()

    emb = TitaNetEmbedder(Path(args.model))
    truth = json.loads(Path(args.truth).read_text())

    worst = 1.0
    for wav, ref in truth.items():
        ref = np.asarray(ref, dtype=np.float32)
        got = emb.embed(read_wav_16k(wav))
        cos = float(np.dot(ref, got) /
                    ((np.linalg.norm(ref) * np.linalg.norm(got)) or 1e-9))
        worst = min(worst, cos)
        print(f"{'OK ' if cos >= 0.995 else 'FAIL'} cos={cos:.5f}  {wav}")

    print(f"\nworst cosine: {worst:.5f}")
    sys.exit(0 if worst >= 0.995 else 1)


if __name__ == "__main__":
    main()
