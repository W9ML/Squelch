"""Watchlist: alert when a transmission matches a saved rule.

Rules (from the DB) are matched against a finished transmission record.
A hit fires an in-browser notification (via a WebSocket event that the
open dashboard turns into a Notification) and, optionally, a webhook
POST for real push to a phone (ntfy, Discord, Home Assistant, ...).

Rule kinds:
    callsign   - value appears as a callsign in the transcript or the
                 assigned speaker's label
    mdc_unit   - an MDC burst with that unit id was decoded
    emergency  - any MDC Emergency (op type 'E')
    speaker    - the transmission was assigned to that speaker id
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from .callsigns import extract_callsigns

log = logging.getLogger(__name__)


def url_is_safe(url: str) -> bool:
    """SSRF guard for webhook targets: allow only http(s) URLs that resolve
    to public IP addresses. Blocks loopback, private, and link-local ranges
    (incl. the cloud metadata address 169.254.169.254) and other reserved
    space, so an admin can't turn the server into an internal-network proxy."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port,
                                   proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return True


def match(record: dict, rules: list[dict]) -> list[dict]:
    """Return the rules a transmission record hits (pure, testable)."""
    transcript = record.get("transcript") or ""
    calls = {c.upper() for c in extract_callsigns(transcript)}
    label = record.get("speaker_label") or ""
    calls |= {c.upper() for c in extract_callsigns(label)}
    # Say Again ([sayagain] on): also match the cross-source-RESOLVED call, so a
    # watch on the real call still fires when Whisper garbled it in the
    # transcript ("W9DT" -> "W9DTE"). Only the asserted `resolved` — never the
    # tentative `alt` of an uncertain match, which would fire false alerts. The
    # raw extractions above stay in the set, so nothing that matched before
    # stops matching, and this is a no-op when the feature is off.
    for rc in (record.get("callsigns_resolved") or []):
        res = rc.get("resolved")
        if res:
            calls.add(res.upper())
    mdc = record.get("mdc") or []
    units = {str(m.get("unit_raw") or m.get("unit_id_hex") or "") for m in mdc}
    has_emergency = any(m.get("type") == "E" for m in mdc)
    speaker_id = record.get("speaker_id")

    hits = []
    for r in rules:
        kind, value = r["kind"], (r.get("value") or "")
        hit = False
        reason = ""
        if kind == "callsign" and value.upper() in calls:
            hit, reason = True, f"heard {value}"
        elif kind == "mdc_unit" and value in units:
            hit, reason = True, f"MDC unit {value}"
        elif kind == "emergency" and has_emergency:
            hit, reason = True, "MDC EMERGENCY"
        elif kind == "speaker" and str(speaker_id) == value:
            hit, reason = True, f"{label or 'speaker'} keyed up"
        if hit:
            hits.append({"watch_id": r["id"],
                         "username": r.get("username"),
                         "label": r.get("label") or reason,
                         "reason": reason, "kind": kind,
                         "webhook": r.get("webhook") or ""})
    return hits


def send_webhook(url: str, title: str, body: str) -> None:
    """Best-effort webhook POST. ntfy accepts a plain body; most others
    accept JSON — send JSON and let the receiver adapt."""
    import json
    import urllib.request
    if not url_is_safe(url):
        log.warning("refusing webhook to unsafe or non-public URL: %s", url)
        return
    try:
        data = json.dumps({"title": title, "message": body,
                           "content": body}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Title", title)          # ntfy reads this header
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as e:
        log.warning("watch webhook failed (%s): %s", url, e)
