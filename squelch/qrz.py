"""Callsign enrichment via the QRZ XML Data API.

Richer than the free FCC feed: adds the operator's email and QRZ profile
photo, and covers DX callsigns. Requires a QRZ XML Data (or premium)
subscription on the account.

Authentication is the user's QRZ LOGIN (username + password): the XML API
exchanges it for a short-lived session key, which this module fetches and
refreshes automatically. Note this is distinct from the API key shown under
Logbook -> Settings on qrz.com — that key belongs to QRZ's Logbook (ADIF
upload) API and cannot pull biographical data.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

_URL = "https://xmldata.qrz.com/xml/current/"
_AGENT = "squelch-1.0"
_TIMEOUT = 8.0

# process-wide session key; the XML API expires these periodically and we
# transparently re-login when it does
_session_key: str | None = None


def reset_session() -> None:
    """Drop the cached session key (call after credentials change)."""
    global _session_key
    _session_key = None


def _fetch(params: dict) -> ET.Element | None:
    url = _URL + "?" + urllib.parse.urlencode({**params, "agent": _AGENT})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return ET.fromstring(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as e:
        log.info("qrz request failed: %s", e)
        return None


def _child_text(el: ET.Element | None, name: str) -> str:
    """Namespace-agnostic child lookup (QRZ XML carries an xmlns)."""
    if el is None:
        return ""
    for c in el:
        if c.tag.rsplit("}", 1)[-1] == name:
            return (c.text or "").strip()
    return ""


def _section(root: ET.Element | None, name: str) -> ET.Element | None:
    if root is None:
        return None
    for c in root:
        if c.tag.rsplit("}", 1)[-1] == name:
            return c
    return None


def _login(username: str, password: str) -> str | None:
    root = _fetch({"username": username, "password": password})
    session = _section(root, "Session")
    key = _child_text(session, "Key")
    if not key:
        log.warning("qrz login failed: %s",
                    _child_text(session, "Error") or "no session key")
        return None
    return key


_CLASS = {"E": "Extra", "A": "Advanced", "G": "General",
          "T": "Technician", "N": "Novice", "C": "Club"}


def parse_callsign(cs_el: ET.Element) -> dict:
    """Shape a <Callsign> element into the fields the UI shows."""
    name = " ".join(
        p for p in (_child_text(cs_el, "fname"), _child_text(cs_el, "name"))
        if p).title()
    cls = _child_text(cs_el, "class")
    return {
        "status": "found",
        "name": name,
        "type": "club" if cls == "C" else "person",
        "opclass": _CLASS.get(cls, cls.title()),
        "city": _child_text(cs_el, "addr2").title(),
        "state": _child_text(cs_el, "state").upper(),
        "grid": _child_text(cs_el, "grid").upper(),
        "email": _child_text(cs_el, "email"),
        "image": _child_text(cs_el, "image"),
        "source": "qrz",
    }


def lookup(username: str, password: str, callsign: str) -> dict:
    """Fetch and shape enrichment for one callsign. Never raises. Retries
    exactly once through a fresh login when the session key has expired."""
    global _session_key
    for _attempt in (1, 2):
        if not _session_key:
            _session_key = _login(username, password)
            if not _session_key:
                return {"status": "error"}
        root = _fetch({"s": _session_key, "callsign": callsign})
        if root is None:
            return {"status": "error"}
        err = _child_text(_section(root, "Session"), "Error").lower()
        if err and ("session" in err or "invalid" in err or "expired" in err) \
                and "not found" not in err:
            reset_session()                      # stale key -> re-login once
            continue
        if err and "not found" in err:
            return {"status": "not_found"}
        cs_el = _section(root, "Callsign")
        if cs_el is None:
            # no callsign payload and no recognizable error: subscription
            # missing or service hiccup — treat as an error (retried later)
            log.info("qrz lookup for %s returned no data (%s)",
                     callsign, err or "no error text")
            return {"status": "error"}
        return parse_callsign(cs_el)
    return {"status": "error"}
