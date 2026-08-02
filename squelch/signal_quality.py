"""Estimate the received signal quality of a transmission from its audio.

Radio audio carries its own quality tell-tales: the ratio of speech
energy to the squelch/noise floor (SNR), hard clipping, and the rapid
level swings of a fluttering mobile ("picket fencing"). None of this is
ground truth about RF SNR — it's an audio-domain proxy — but it's a
useful "was this a good copy?" grade and it correlates with the voter
RSSI when that's available.
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 8000


def score(audio: np.ndarray, win_ms: int = 20) -> dict | None:
    """audio: int16 mono @ 8 kHz. Returns
    {snr, label, clipping, flutter} or None for empty input."""
    if audio is None or len(audio) == 0:
        return None
    win = int(SAMPLE_RATE * win_ms / 1000)
    n = len(audio) // win
    if n < 3:
        return None
    f = audio[:n * win].astype(np.float64).reshape(n, win)
    rms = np.sqrt((f ** 2).mean(axis=1)) + 1e-9

    # noise floor from the quietest windows, speech from the loud ones
    noise = np.percentile(rms, 15)
    speech = np.percentile(rms, 85)
    snr = 20.0 * np.log10(max(speech, 1.0) / max(noise, 1.0))
    snr = float(np.clip(snr, 0.0, 45.0))

    # clipping: fraction of samples pinned near int16 full scale
    clip_frac = float((np.abs(audio.astype(np.int32)) > 32000).mean())

    # flutter: how much the *loud* (speech) windows swing in level —
    # a steady signal is smooth, a fluttering mobile is jagged
    active = rms[rms > max(noise * 3, speech * 0.3)]
    flutter = float(active.std() / (active.mean() + 1e-9)) if len(active) > 4 else 0.0

    if clip_frac > 0.02:
        label = "clipping"
    elif snr < 8:
        label = "noisy"
    elif flutter > 0.85:
        label = "flutter"
    elif snr < 16:
        label = "fair"
    elif snr < 26:
        label = "good"
    else:
        label = "excellent"

    return {"snr": round(snr, 1), "label": label,
            "clipping": clip_frac > 0.02, "flutter": round(flutter, 2)}
