"""Fuzzy transmitter geolocation from voter receiver RSSI (v2).

Replaces the old free-space trilateration with a censored, ERP-profiled,
grid-evaluated posterior. Three facts drive the rewrite, all confirmed by an
independent review of the physics:

  * RSSI here is a *quieting* (SNR) proxy that SATURATES. A site pinned at 255
    only says "at least this strong" -> an upper bound on distance, never a
    point range. The old code inverted 255 to a 1.5 km ring at MAXIMUM weight,
    so two saturated sites 32 km apart each claimed the TX was 1.5 km away and
    the fit split the difference: a confident answer pointing at a cornfield.
  * A site that hears *nothing* is evidence too (the TX is probably not sitting
    on top of it). The old code discarded rssi == 0 rather than using it.
  * The transmit power is unknown across ~35 dB, so absolute loss cannot be
    inverted to distance. But power enters as a single constant common to every
    site, which cancels exactly when it is profiled out. What is left is the
    *pattern* of level differences across sites, evaluated on a grid.

Still entertainment, not direction finding: VHF shadowing (~7 dB, frozen per
path) and saturation put the honest floor at kilometres. The estimate now
carries its own credible radius; render the region, never a bare pin.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.special import log_ndtr          # numerically stable log(Phi(z))

_EARTH_R = 6371000.0                          # metres

# --- model constants (physics, not free tuning knobs) ---
_N_EXP = 3.5                 # VHF/UHF path-loss exponent (~35 dB/decade), not 2
_SIGMA_DB = 7.0             # per-site shadowing; keeps credible regions honest
_SIGMA_SILENT_DB = 12.0    # a silent site is weak evidence (rssi 0 is ambiguous)
_NU = 4.0                  # Student-t dof: one dead preamp must not move the fix
_SAT_RSSI = 250            # median >= this  -> saturated (right-censored)
_FLOOR_RSSI = 2            # peak   <= this  -> silent   (left-censored/exclusion)
_DMIN_KM = 0.3             # distance floor so the log-loss can't blow up
_PAD_KM = 25.0             # grid extends this far beyond the receiver hull
_STEP_KM = 0.5             # nominal grid resolution
_MAX_AXIS = 260            # cap cells per axis (auto-coarsens a huge area)
_MAX_TRACK = 600           # cap animation frames (halo sync stays smooth enough)

# monotone rssi -> relative quieting level (dB), from the RTCM quieting curve.
# Only the shape/spacing matters: the additive offset is profiled out anyway.
_Q_RSSI = np.array([0, 1, 5, 10, 30, 50, 56, 60, 100, 150,
                    200, 230, 245, 250, 254, 255], float)
_Q_DB = np.array([-0.4, 0.43, 1.38, 2.52, 7.23, 11.65, 13.0, 13.28, 15.23,
                  18.59, 24.13, 30.80, 38.27, 43.53, 53.07, 56.0], float)
_Q_SAT = float(np.interp(_SAT_RSSI, _Q_RSSI, _Q_DB))
_Q_FLOOR = float(np.interp(_FLOOR_RSSI, _Q_RSSI, _Q_DB))

# Student-t log-pdf constant (per unit sigma)
_T_C = (math.lgamma((_NU + 1) / 2) - math.lgamma(_NU / 2)
        - 0.5 * math.log(_NU * math.pi))


def _q_of(rssi):
    return np.interp(rssi, _Q_RSSI, _Q_DB)


def _enu(lat, lon, lat0, lon0):
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * _EARTH_R
    y = math.radians(lat - lat0) * _EARTH_R
    return x, y


def _enu_inv(x, y, lat0, lon0):
    lat = lat0 + math.degrees(y / _EARTH_R)
    lon = lon0 + math.degrees(x / (_EARTH_R * math.cos(math.radians(lat0))))
    return lat, lon


def _student_t_ll(resid, sigma):
    """Vectorised Student-t log-likelihood — robust to a per-site outlier."""
    t2 = (resid / sigma) ** 2
    return _T_C - math.log(sigma) - 0.5 * (_NU + 1.0) * np.log1p(t2 / _NU)


def _build_track(samples):
    """Per-frame RSSI for the audio-synced halos. `est` is None on every frame:
    sub-over motion is unobservable through the shadowing, so the marker stays
    put at the whole-over estimate instead of jittering (the frontend already
    guards `if (s.est)`)."""
    n = len(samples)
    idx = range(n)
    if n > _MAX_TRACK:                        # decimate frames, keep timing
        step = n / _MAX_TRACK
        idx = sorted({int(i * step) for i in range(_MAX_TRACK)})
    track = []
    for i in idx:
        t = samples[i][0]
        acc: dict[str, list] = {}
        for tt, row in samples:
            if t - 4.0 <= tt <= t:            # 4 s trailing average
                for nm, v in row.items():
                    acc.setdefault(nm, []).append(v)
        track.append({"t": t, "est": None,
                      "rssi": {nm: round(sum(v) / len(v)) for nm, v in acc.items()}})
    return track


def estimate_track(voter: dict, coords: dict) -> dict | None:
    """Geo estimate for the UI map from a transmission's voter samples.
    `coords` maps receiver name -> [lat, lon].

    Returns {receivers, track, best_est, distances, credible_km, confidence,
    method} or None if no receiver with known coordinates ever had signal.
    """
    if not voter or not coords:
        return None

    # ---- gather per-time rssi rows for receivers we have coordinates for ----
    samples: list[tuple[float, dict]] = []
    present: set[str] = set()
    for nd in voter.get("nodes", []):
        names = nd.get("clients", [])
        for s in nd.get("samples", []):
            t, rssi_list = s[0], s[1]
            row = {}
            for i in range(min(len(names), len(rssi_list))):
                nm = names[i]
                if nm in coords:
                    present.add(nm)
                    row[nm] = rssi_list[i]
            if row:
                samples.append((t, row))
    if not samples:
        return None
    samples.sort(key=lambda x: x[0])

    used = [n for n in coords if n in present]        # stable, config order
    if not used:
        return None

    # ---- per-site summary + censoring class ----
    peak, rep = {}, {}
    for n in used:
        vals = [row[n] for _, row in samples if n in row]
        nz = [v for v in vals if v > 0]
        peak[n] = max(vals) if vals else 0
        rep[n] = float(np.median(nz)) if nz else 0.0
    saturated = [n for n in used if rep[n] >= _SAT_RSSI]
    silent = [n for n in used if peak[n] <= _FLOOR_RSSI]
    measured = [n for n in used if n not in saturated and n not in silent]

    # nobody actually heard the TX (only silent sites) -> nothing to locate
    if not measured and not saturated:
        return None

    track = _build_track(samples)
    receivers = [{"name": n, "lat": coords[n][0], "lon": coords[n][1]}
                 for n in used]

    # ---- metric grid over the receiver hull, padded ----
    lat0 = sum(coords[n][0] for n in used) / len(used)
    lon0 = sum(coords[n][1] for n in used) / len(used)
    enu = {n: _enu(coords[n][0], coords[n][1], lat0, lon0) for n in used}
    xs = [enu[n][0] for n in used]
    ys = [enu[n][1] for n in used]
    pad = _PAD_KM * 1000.0
    xmin, xmax = min(xs) - pad, max(xs) + pad
    ymin, ymax = min(ys) - pad, max(ys) + pad
    step = _STEP_KM * 1000.0
    nx = int((xmax - xmin) / step) + 1
    ny = int((ymax - ymin) / step) + 1
    while nx > _MAX_AXIS or ny > _MAX_AXIS:
        step *= 1.5
        nx = int((xmax - xmin) / step) + 1
        ny = int((ymax - ymin) / step) + 1
    gx = xmin + step * np.arange(nx)
    gy = ymin + step * np.arange(ny)
    GX, GY = np.meshgrid(gx, gy)                       # (ny, nx)
    cell_km2 = (step / 1000.0) ** 2

    L = {n: 10.0 * _N_EXP * np.log10(
        np.maximum(np.sqrt((GX - enu[n][0]) ** 2 + (GY - enu[n][1]) ** 2)
                   / 1000.0, _DMIN_KM)) for n in used}

    # ---- log-posterior over the grid ----
    if measured:
        # profile out the unknown common level A from the measured sites
        q = {n: float(_q_of(rep[n])) for n in measured}
        Ahat = sum(q[n] + L[n] for n in measured) / len(measured)
        logP = np.zeros_like(GX)
        for n in measured:                    # power-invariant measurement term
            logP += _student_t_ll((q[n] + L[n]) - Ahat, _SIGMA_DB)
    else:
        # no uncensored site: anchor A to the saturation threshold so the
        # censored terms still shape a (low-confidence, broad) region
        Ahat = _Q_SAT + (L[saturated[0]] if saturated else 0.0)
        logP = np.zeros_like(GX)

    for n in saturated:                        # observed >= Q_SAT: bound to near
        logP += log_ndtr((Ahat - L[n] - _Q_SAT) / _SIGMA_DB)
    for n in silent:                           # heard nothing: push away (weak)
        logP += log_ndtr((_Q_FLOOR - (Ahat - L[n])) / _SIGMA_SILENT_DB)

    logP -= float(logP.max())
    post = np.exp(logP)
    tot = float(post.sum())
    if not np.isfinite(tot) or tot <= 0:
        return None
    post /= tot

    # MAP cell (a real probable location, never a between-modes valley)
    mi = int(np.argmax(post))
    best_x, best_y = float(GX.flat[mi]), float(GY.flat[mi])
    blat, blon = _enu_inv(best_x, best_y, lat0, lon0)
    best_est = [round(blat, 6), round(blon, 6)]

    # 68% highest-density area -> equivalent credible radius
    flat = np.sort(post.ravel())[::-1]
    cum = np.cumsum(flat)
    k = int(np.searchsorted(cum, 0.68)) + 1
    credible_km = round(math.sqrt(k * cell_km2 / math.pi), 1)

    distances = {n: round(math.hypot(enu[n][0] - best_x,
                                     enu[n][1] - best_y) / 1000.0, 1)
                 for n in used}

    nmeas = len(measured)
    if nmeas >= 3 and credible_km <= 6.0:
        confidence = "high"
    elif nmeas >= 2 and credible_km <= 14.0:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "receivers": receivers,
        "track": track,
        "best_est": best_est,
        "distances": distances,
        "credible_km": credible_km,
        "confidence": confidence,
        "n_measured": nmeas,
        "n_saturated": len(saturated),
        "n_silent": len(silent),
        "method": "censored-grid-v2",
    }
