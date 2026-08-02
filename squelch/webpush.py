"""Web Push (VAPID) delivery for watchlist alerts.

Optional dependency: if ``pywebpush`` isn't installed the module degrades to a
no-op so the rest of the app runs unchanged (same pattern as the optional voice
embedders). VAPID keys are generated once with ``cryptography`` (always present)
and cached in the settings table; the browser subscribes with the public
application-server key and the server signs pushes with the private scalar.
"""

from __future__ import annotations

import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

log = logging.getLogger(__name__)

try:
    from pywebpush import WebPushException, webpush
    _HAVE_PYWEBPUSH = True
except Exception:  # pragma: no cover - depends on optional install
    _HAVE_PYWEBPUSH = False

# VAPID 'sub' claim: must be a mailto: or https: URL, but push services don't
# verify it — it's only a contact hint. Kept generic to avoid leaking an address.
VAPID_SUB = "mailto:noreply@localhost"


def available() -> bool:
    """True when web push can actually be sent (pywebpush importable)."""
    return _HAVE_PYWEBPUSH


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def get_or_create_keys(db) -> tuple[str, str]:
    """Return ``(application_server_key_b64url, private_key_b64url)``.

    The public value is the uncompressed EC point the browser passes as
    ``applicationServerKey``; the private value is the 32-byte scalar in the
    base64url form ``py_vapid`` expects. Generated and persisted on first call.
    """
    pub = db.get_setting("vapid_public_key")
    priv = db.get_setting("vapid_private_key")
    if pub and priv:
        return pub, priv

    key = ec.generate_private_key(ec.SECP256R1())
    priv_scalar = key.private_numbers().private_value.to_bytes(32, "big")
    raw_pub = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    pub = _b64url(raw_pub)
    priv = _b64url(priv_scalar)
    db.set_setting("vapid_public_key", pub)
    db.set_setting("vapid_private_key", priv)
    return pub, priv


def send_to_user(db, username: str, title: str, body: str,
                 url: str = "/", tag: str = "squelch") -> int:
    """Push only to one user's subscriptions (per-user alerts)."""
    if not username:
        return 0
    return _send(db, db.list_push_subscriptions(username), title, body, url, tag)


def send_to_all(db, title: str, body: str, url: str = "/", tag: str = "squelch") -> int:
    """Push a notification to every stored subscription. Returns the count
    delivered; no-op (0) when pywebpush isn't installed."""
    return _send(db, db.list_push_subscriptions(), title, body, url, tag)


def _send(db, subs: list[dict], title: str, body: str,
          url: str, tag: str) -> int:
    """Deliver to the given subscriptions, pruning any the push service
    reports as gone (404/410)."""
    if not _HAVE_PYWEBPUSH or not subs:
        return 0
    _, private_key = get_or_create_keys(db)
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    sent = 0
    for s in subs:
        info = {
            "endpoint": s["endpoint"],
            "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
        }
        try:
            webpush(
                subscription_info=info,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": VAPID_SUB},
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                db.delete_push_subscription(s["endpoint"])
                log.info("pruned dead push subscription (%s)", code)
            else:
                log.warning("web push failed: %s", e)
        except Exception as e:  # pragma: no cover - network/library errors
            log.warning("web push error: %s", e)
    return sent
