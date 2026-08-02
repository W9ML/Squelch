#!/usr/bin/env python3
"""One-time TitaNet-Large ONNX export. Run in a throwaway NeMo venv:

    python tools/export_titanet.py --out /var/lib/squelch/models \
        [--truth-wavs /var/lib/squelch/audio/.../tx_*.wav ...]

Writes titanet_large.onnx plus (optionally) titanet_truth.json holding
NeMo-computed embeddings for the given wavs — tools/validate_titanet.py
replays those through the app's own featurizer+onnxruntime and checks the
cosines, proving the reimplementation matches before the engine goes live.
"""

import argparse
import json
from pathlib import Path

import numpy as np


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--truth-wavs", nargs="*", default=[],
                    help="wav files to embed with NeMo as ground truth")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    import torch  # noqa: F401  (ensures CPU torch is importable first)
    from nemo.collections.asr.models import EncDecSpeakerLabelModel

    print("loading titanet_large (downloads from NGC on first run)…")
    model = EncDecSpeakerLabelModel.from_pretrained("titanet_large")
    model.eval()

    onnx_path = out / "titanet_large.onnx"
    model.export(str(onnx_path))
    print(f"exported {onnx_path} ({onnx_path.stat().st_size // 1_000_000} MB)")

    if args.truth_wavs:
        truth = {}
        for w in args.truth_wavs:
            emb = model.get_embedding(w).cpu().numpy().reshape(-1)
            truth[w] = emb.astype(float).tolist()
            print(f"  truth: {w} -> {len(emb)}-d")
        tp = out / "titanet_truth.json"
        tp.write_text(json.dumps(truth))
        print(f"wrote {tp}")


if __name__ == "__main__":
    main()
