"""Speaker identification by voice-embedding similarity.

Embedder backends (auto-selected, config-overridable):
  * TitaNet-Large via ONNX Runtime (explicit opt-in, GPU-ready) — 192-dim,
    the strongest separation on band-limited FM; needs the exported
    titanet_large.onnx in the models dir (see tools/export_titanet.py).
  * ECAPA-TDNN via speechbrain — 192-dim, far better speaker separation
    than the old GE2E model, CPU-friendly.
  * resemblyzer GE2E (fallback) — 256-dim, kept so the feature degrades
    gracefully when nothing else is installed.

TitaNet and ECAPA are both 192-dim but live in DIFFERENT embedding spaces —
switching engines requires wiping/re-enrolling profiles (dimension filtering
alone cannot tell them apart).

Profiles are SETS of embeddings (speaker_embeddings table, capped), not a
single running-mean centroid: one operator sounds different across radios
and channels, and a mean smears that into "average radio voice". A match
score against a speaker is the mean of the top-K cosines to their set.

Raw cosines on band-limited FM audio are inflated by the shared channel
(everyone sounds a bit alike through a repeater), so the decision leans on
the MARGIN between the best speaker and the runner-up, not the absolute
cosine: a candidate must stand out from the OTHER known speakers, not just
score high. The assign / suggest / abstain call lives in the pipeline
(speakerid.voice_decision): assign only when the top cosine clears a high
bar AND out-margins the runner-up, suggest in a middle band, else abstain —
an Unknown beats a wrong name. (There is no cohort AS-norm and no logistic
calibration — raw top-K cosine plus the margin gate is the whole model.)

This module is deliberately side-effect-light: evaluate() is pure scoring;
creating auto-clusters and enrolling samples are explicit calls owned by
the pipeline's identity-resolution step.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

try:
    from speechbrain.inference.speaker import EncoderClassifier
    _HAS_ECAPA = True
except ImportError:
    try:  # older speechbrain layout
        from speechbrain.pretrained import EncoderClassifier  # type: ignore
        _HAS_ECAPA = True
    except ImportError:
        EncoderClassifier = None
        _HAS_ECAPA = False

try:
    from resemblyzer import VoiceEncoder
    _HAS_RESEMBLYZER = True
except ImportError:
    VoiceEncoder = None
    _HAS_RESEMBLYZER = False

from . import titanet as _titanet

_ECAPA_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
_TITANET_FILE = "titanet_large.onnx"
_TITANET_DIM = 192
_ECAPA_DIM = 192
_GE2E_DIM = 256

# scoring: mean of the top-K cosines against a speaker's embedding set —
# robust to one bad enrolled sample without requiring every sample to match
_TOP_K = 3


def _top_k_mean(sims: np.ndarray, k: int = _TOP_K) -> float:
    if len(sims) == 0:
        return -1.0
    k = min(k, len(sims))
    return float(np.sort(sims)[-k:].mean())


def voice_decision(ev: dict | None, assign_cos: float, assign_margin: float,
                   suggest_cos: float):
    """The assign / suggest / abstain call from a voice-evaluation dict.

    Single source of truth shared by the live pipeline (Pipeline._voice_call)
    and the offline eval harness (tools/eval_harness.py) so the two can never
    drift. Returns (decision, speaker_id, cosine): decision is 'assign',
    'suggest', or None (abstain). 'assign' needs the top cosine to clear
    assign_cos AND out-margin the runner-up by assign_margin — the margin is
    what stops two similar profiles both claiming a voice.
    """
    if not ev or ev["best_id"] is None:
        return None, None, 0.0
    best, cos, margin = ev["best_id"], ev["best_cos"], ev["margin"]
    if cos >= assign_cos and margin >= assign_margin:
        return "assign", best, cos
    if cos >= suggest_cos:
        return "suggest", best, cos
    return None, best, cos


class SpeakerIdentifier:
    def __init__(self, db, match_threshold: float, autocluster: bool,
                 learn_threshold: float | None = None,
                 backend: str = "auto"):
        self.db = db
        # legacy thresholds kept for the resemblyzer fallback path
        self.match_threshold = match_threshold
        self.learn_threshold = learn_threshold or max(0.92, match_threshold)
        self.autocluster = autocluster
        self._encoder = None
        self._lock = threading.Lock()
        if backend == "titanet":
            # explicit opt-in only: same dim as ECAPA but a different space,
            # so it must never be silently swapped in by auto-selection
            self.backend = "titanet" if _titanet.HAS_ORT else None
            if self.backend is None:
                log.error("embedder 'titanet' requested but onnxruntime "
                          "is not installed — voice ID disabled")
        elif backend == "ecapa":
            self.backend = "ecapa" if _HAS_ECAPA else None
        elif backend == "resemblyzer":
            self.backend = "resemblyzer" if _HAS_RESEMBLYZER else None
        else:                                   # auto
            self.backend = ("ecapa" if _HAS_ECAPA
                            else "resemblyzer" if _HAS_RESEMBLYZER else None)
        self.model_dir = None                   # set by pipeline (data_dir)

    @property
    def available(self) -> bool:
        return self.backend is not None

    @property
    def embedding_dim(self) -> int:
        if self.backend == "titanet":
            return _TITANET_DIM
        return _ECAPA_DIM if self.backend == "ecapa" else _GE2E_DIM

    # ---- embedding ----

    def _get_encoder(self):
        with self._lock:
            if self._encoder is None:
                if self.backend == "titanet":
                    path = (self.model_dir or Path(".")) / _TITANET_FILE
                    try:
                        self._encoder = _titanet.TitaNetEmbedder(path)
                    except Exception:
                        log.exception("titanet model failed to load (%s) — "
                                      "voice ID disabled", path)
                        self.backend = None
                        return None
                elif self.backend == "ecapa":
                    log.info("loading voice encoder (ECAPA-TDNN)")
                    kwargs = {"run_opts": {"device": "cpu"}}
                    if self.model_dir:
                        kwargs["savedir"] = str(
                            self.model_dir / "ecapa-voxceleb")
                    self._encoder = EncoderClassifier.from_hparams(
                        source=_ECAPA_SOURCE, **kwargs)
                else:
                    log.info("loading voice encoder (resemblyzer)")
                    self._encoder = VoiceEncoder("cpu", verbose=False)
            return self._encoder

    def embed(self, audio_16k: np.ndarray) -> np.ndarray | None:
        """audio_16k: float32 mono 16 kHz in [-1, 1]. Blocking."""
        if not self.available:
            return None
        peak = float(np.max(np.abs(audio_16k))) or 1.0
        wav = (audio_16k / peak).astype(np.float32)
        if self.backend == "titanet":
            enc = self._get_encoder()
            return enc.embed(wav) if enc is not None else None
        if self.backend == "ecapa":
            import torch
            with torch.no_grad():
                t = torch.from_numpy(wav).unsqueeze(0)
                emb = self._get_encoder().encode_batch(t)
            return emb.squeeze().cpu().numpy().astype(np.float32)
        emb = self._get_encoder().embed_utterance(wav)
        return emb.astype(np.float32)

    # ---- scoring (pure; no side effects) ----

    def evaluate(self, embedding: np.ndarray) -> dict:
        """Score an embedding against every known speaker profile.

        Pure cosine scoring — no side effects, no model needed — so it stays
        testable on a box without the embedder. Returns each speaker's top-K
        cosine plus the best match and its MARGIN over the runner-up. The
        assign / suggest / abstain decision lives in the pipeline
        (threshold + margin), which is far more debuggable and tunable than
        a calibrated probability, and the margin is what stops several
        speakers collapsing onto one name.

            {"raw": {speaker_id: cosine}, "best_id", "best_cos",
             "second_cos", "margin"}
        """
        return self.evaluate_against(
            embedding, self.db.load_speaker_profiles(len(embedding)))

    def evaluate_against(self, embedding: np.ndarray, profiles) -> dict:
        """Same scoring as evaluate(), but against a PRE-LOADED profile set
        (a list of (speaker_id, embedding_matrix) from load_speaker_profiles).
        A batch caller that scores many embeddings against the same profiles
        loads them once instead of once per row — see revisit_unassigned."""
        e = embedding / (np.linalg.norm(embedding) or 1e-9)
        raw: dict[int, float] = {}
        for spk_id, mat in profiles:
            norms = np.linalg.norm(mat, axis=1)
            norms[norms == 0] = 1e-9
            sims = (mat / norms[:, None]) @ e
            raw[spk_id] = _top_k_mean(sims)
        if not raw:
            return {"raw": {}, "best_id": None, "best_cos": 0.0,
                    "second_cos": 0.0, "margin": 0.0}
        order = sorted(raw.items(), key=lambda kv: kv[1], reverse=True)
        best_id, best_cos = order[0]
        second_cos = order[1][1] if len(order) > 1 else -1.0
        return {"raw": raw, "best_id": best_id, "best_cos": float(best_cos),
                "second_cos": float(second_cos),
                "margin": float(best_cos - second_cos)}

    # ---- profile mutation (explicit; called by the pipeline/UI only) ----

    def enroll(self, speaker_id: int, embedding: np.ndarray,
               tx_id: int | None = None, verified: bool = False) -> None:
        """Add a sample to a speaker's voice profile."""
        self.db.add_speaker_embedding(speaker_id, embedding,
                                      tx_id=tx_id, verified=verified)

    def create_cluster(self, embedding: np.ndarray,
                       tx_id: int | None = None) -> int:
        """New auto-numbered speaker for an unrecognized voice."""
        label = f"Speaker {self.db.next_auto_speaker_number()}"
        spk_id = self.db.create_speaker(label, embedding)
        self.db.add_speaker_embedding(spk_id, embedding, tx_id=tx_id)
        return spk_id
