"""Callsign enrichment via callook.info (US FCC amateur license data).

A single small JSON GET per callsign, cached in the database so we hit
the service at most once per callsign per cache window. US-only: DX and
invalid callsigns come back as ``not_found``; a network hiccup is
``error`` (retried on the next lookup). No API key required.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

_URL = "https://callook.info/{}/json"
_UA = "Squelch/0.1 (+https://github.com/ ham radio node monitor)"
_TIMEOUT = 8.0


def _parse_city_state(line2: str) -> tuple[str, str]:
    """'VALPARAISO, IN 46383' -> ('Valparaiso', 'IN')."""
    if "," not in line2:
        return "", ""
    city, _, rest = line2.rpartition(",")
    parts = rest.split()
    state = parts[0] if parts else ""
    return city.strip().title(), state.strip().upper()


def parse(raw: dict) -> dict:
    """Shape a raw callook payload into the fields the UI shows."""
    if not isinstance(raw, dict) or raw.get("status") != "VALID":
        return {"status": "not_found"}
    cur = raw.get("current") or {}
    loc = raw.get("location") or {}
    addr = raw.get("address") or {}
    city, state = _parse_city_state(addr.get("line2", ""))
    return {
        "status": "found",
        "name": (raw.get("name") or "").title(),
        "type": (raw.get("type") or "").lower(),      # 'person' | 'club'
        "opclass": (cur.get("operClass") or "").title(),
        "city": city,
        "state": state,
        "grid": (loc.get("gridsquare") or "").upper(),
    }


def lookup(callsign: str) -> dict:
    """Fetch and shape enrichment for one callsign. Never raises."""
    try:
        req = urllib.request.Request(_URL.format(callsign),
                                     headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log.info("callook lookup failed for %s: %s", callsign, e)
        return {"status": "error"}
    return parse(raw)
