"""TitaNet-Large speaker embeddings via ONNX Runtime.

The .nemo checkpoint is exported ONCE to ONNX (tools/export_titanet.py, run
in a throwaway NeMo venv) and the app runs it through onnxruntime — no NeMo,
no torch needed at inference. The exported graph takes preprocessed features,
so this module reimplements NeMo's AudioToMelSpectrogramPreprocessor for
titanet_large exactly (validated against NeMo-computed embeddings by
tools/validate_titanet.py; keep both in sync if any constant changes):

    16 kHz mono -> preemphasis 0.97 -> STFT (n_fft 512, win 400 hann,
    hop 160, center/reflect) -> |.|^2 -> 80-bin mel (slaney) ->
    log(x + 2^-24) -> per-feature mean/std normalization

Embeddings are 192-dim; cosine geometry differs from both resemblyzer
(256-d) and ECAPA (192-d) — profiles are engine-specific and must never mix.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
_N_FFT = 512
_WIN = 400          # 25 ms
_HOP = 160          # 10 ms
_N_MELS = 80
_PREEMPH = 0.97
_LOG_GUARD = 2.0 ** -24
_NORM_GUARD = 1e-5  # NeMo CONSTANT added to std in per-feature normalization

try:
    import onnxruntime as ort
    HAS_ORT = True
except ImportError:
    ort = None
    HAS_ORT = False


def _hz_to_mel(f: np.ndarray | float) -> np.ndarray | float:
    """Slaney mel scale (librosa default): linear below 1 kHz, log above."""
    f = np.asarray(f, dtype=np.float64)
    mel = f / (200.0 / 3.0)
    log_region = f >= 1000.0
    mel = np.where(
        log_region,
        15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) / (np.log(6.4) / 27.0),
        mel,
    )
    return mel


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float64)
    f = m * (200.0 / 3.0)
    log_region = m >= 15.0
    f = np.where(log_region, 1000.0 * np.exp((np.log(6.4) / 27.0) * (m - 15.0)), f)
    return f


def mel_filterbank(sr: int = SAMPLE_RATE, n_fft: int = _N_FFT,
                   n_mels: int = _N_MELS) -> np.ndarray:
    """librosa.filters.mel equivalent (htk=False, norm='slaney')."""
    fmax = sr / 2.0
    fft_freqs = np.linspace(0, sr / 2.0, n_fft // 2 + 1)
    mel_pts = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(fmax), n_mels + 2))
    weights = np.zeros((n_mels, n_fft // 2 + 1))
    fdiff = np.diff(mel_pts)
    ramps = mel_pts[:, None] - fft_freqs[None, :]
    for i in range(n_mels):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    # slaney normalization: constant energy per channel
    enorm = 2.0 / (mel_pts[2:n_mels + 2] - mel_pts[:n_mels])
    weights *= enorm[:, None]
    return weights.astype(np.float32)


def features(audio_16k: np.ndarray) -> np.ndarray:
    """NeMo-parity log-mel features: float32 (1, 80, T)."""
    x = np.asarray(audio_16k, dtype=np.float32)
    # preemphasis (first sample kept as-is, matching NeMo)
    x = np.concatenate([x[:1], x[1:] - _PREEMPH * x[:-1]])
    # centered STFT with reflect padding (torch.stft center=True)
    pad = _N_FFT // 2
    x = np.pad(x, pad, mode="reflect")
    n_frames = 1 + (len(x) - _N_FFT) // _HOP
    idx = np.arange(_N_FFT)[None, :] + _HOP * np.arange(n_frames)[:, None]
    frames = x[idx]
    # hann window of the win_length, zero-padded to n_fft (torch semantics)
    win = np.hanning(_WIN + 1)[:-1].astype(np.float32)  # periodic hann
    win = np.pad(win, (_N_FFT - _WIN) // 2)
    spec = np.abs(np.fft.rfft(frames * win, n=_N_FFT, axis=1)) ** 2
    mel = spec @ mel_filterbank().T                      # (T, 80)
    logmel = np.log(mel + _LOG_GUARD)
    # per-feature normalization over time
    mean = logmel.mean(axis=0)
    std = logmel.std(axis=0, ddof=1) + _NORM_GUARD
    logmel = (logmel - mean) / std
    return logmel.T[None].astype(np.float32)             # (1, 80, T)


class TitaNetEmbedder:
    """ONNX TitaNet-Large: embed(audio_16k) -> 192-d float32."""

    DIM = 192

    def __init__(self, model_path: Path):
        self.model_path = Path(model_path)
        providers = []
        avail = ort.get_available_providers()
        if "CUDAExecutionProvider" in avail:
            providers.append("CUDAExecutionProvider")
        providers.append("CPUExecutionProvider")
        self.session = ort.InferenceSession(str(self.model_path),
                                            providers=providers)
        used = self.session.get_providers()[0]
        log.info("titanet ready (%s)", used)
        names = [i.name for i in self.session.get_inputs()]
        self._feat_name = names[0]
        self._len_name = names[1] if len(names) > 1 else None

    def embed(self, audio_16k: np.ndarray) -> np.ndarray:
        feats = features(audio_16k)
        inputs = {self._feat_name: feats}
        if self._len_name:
            inputs[self._len_name] = np.array([feats.shape[2]], dtype=np.int64)
        outs = self.session.run(None, inputs)
        # export emits (logits, embs) or just embs — take the 192-d one
        emb = next(o for o in outs if o.shape[-1] == self.DIM)
        return np.asarray(emb, dtype=np.float32).reshape(-1)
