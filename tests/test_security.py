"""Security hardening: login throttle, SSRF guard, voter redaction,
security headers, webhook validation."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from squelch.config import Config
from squelch.events import Broadcaster, _sanitize
from squelch.web import _LoginThrottle, create_app
from squelch.watchlist import url_is_safe


@pytest.fixture
def client(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.admin_password = "correct horse battery"   # >= 12, bootstrap only
    cfg.usrp_port = 0
    cfg.transcribe_enabled = False
    cfg.speaker_id_enabled = False
    cfg.retention_days = 0
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


def login(client):
    r = client.post("/api/login",
                    json={"username": "admin", "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    return r


# ---- login throttle ----

def test_login_throttle_unit():
    t = _LoginThrottle(max_fails=3, base_lock=5.0)
    now = 1000.0
    assert t.retry_after("1.2.3.4", now) == 0.0
    for _ in range(3):
        t.record_failure("1.2.3.4", now)
    assert t.retry_after("1.2.3.4", now) > 0        # locked after 3 fails
    assert t.retry_after("5.6.7.8", now) == 0.0     # a different IP is free
    t.record_success("1.2.3.4")                     # success clears it
    assert t.retry_after("1.2.3.4", now) == 0.0


def test_login_endpoint_locks_out(client):
    for _ in range(5):                              # default max_fails
        assert client.post("/api/login",
                           json={"password": "nope"}).status_code == 403
    r = client.post("/api/login", json={"password": "nope"})
    assert r.status_code == 429
    assert "retry-after" in r.headers


# ---- SSRF guard on webhook targets ----

@pytest.mark.parametrize("url,ok", [
    ("http://8.8.8.8/topic", True),
    ("https://1.1.1.1/", True),
    ("http://127.0.0.1/", False),                   # loopback
    ("http://169.254.169.254/latest/meta-data/", False),  # cloud metadata
    ("http://10.0.0.5/", False),                    # private
    ("http://192.168.1.1/", False),                 # private
    ("http://[::1]/", False),                       # v6 loopback
    ("http://[fd00::1]/", False),                   # v6 unique-local
    ("ftp://8.8.8.8/", False),                      # wrong scheme
    ("file:///etc/passwd", False),                  # wrong scheme
    ("not a url", False),
])
def test_url_is_safe(url, ok):
    assert url_is_safe(url) is ok


def test_webhook_add_rejects_non_http(client):
    login(client)
    bad = client.post("/api/watchlist",
                      json={"kind": "callsign", "value": "W9ML",
                            "webhook": "file:///etc/passwd"})
    assert bad.status_code == 400
    good = client.post("/api/watchlist",
                       json={"kind": "callsign", "value": "W9ML",
                             "webhook": "https://ntfy.sh/mytopic"})
    assert good.status_code == 200


# ---- voter RSSI is login-gated, mirroring the geolocation it feeds ----

def test_voter_hidden_from_anonymous(client):
    import numpy as np

    db = client.app.state.db
    tx = db.insert_transmission(time.time() - 3, time.time(), 3000)
    db.update_transmission(tx, voter_json=json.dumps({"nodes": [{"node": "46655"}]}))

    # anonymous visitors get NO voter data...
    rows = client.get("/api/transmissions").json()["transmissions"]
    row = next(r for r in rows if r["id"] == tx)
    assert "voter" not in row
    assert "voter" not in client.get(f"/api/transmissions/{tx}").json()["transmission"]
    # ...and the geolocation estimate stays gated the same way
    assert client.get(f"/api/transmissions/{tx}/geo").status_code == 403

    # ...including via the voiceprint-similarity listing (which builds
    # full records straight from the DB)
    emb = np.ones(8, dtype=np.float32).tobytes()
    db.update_transmission(tx, embedding=emb)
    tx2 = db.insert_transmission(time.time() - 2, time.time(), 2000)
    db.update_transmission(tx2, embedding=emb)
    sim = client.get(f"/api/transmissions/{tx2}/similar").json()["transmissions"]
    assert sim and all("voter" not in r for r in sim)

    login(client)
    rows = client.get("/api/transmissions").json()["transmissions"]
    row = next(r for r in rows if r["id"] == tx)
    assert row["voter"] == {"nodes": [{"node": "46655"}]}   # logged in sees it
    sim = client.get(f"/api/transmissions/{tx2}/similar").json()["transmissions"]
    assert any(r.get("voter") for r in sim)                 # admin still sees it


def test_sanitize_strips_voter():
    assert _sanitize(None) is None
    nothing = {"tx": {"id": 1}}
    assert _sanitize(nothing) is nothing            # unchanged -> same object
    out = _sanitize({"tx": {"id": 1, "voter": {"x": 1}}})
    assert "voter" not in out["tx"] and out["tx"]["id"] == 1


def test_broadcaster_redacts_voter_for_anon():
    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send_text(self, m):
            self.sent.append(m)

    b = Broadcaster()
    admin_ws, anon_ws = FakeWS(), FakeWS()
    b.add(admin_ws, is_admin=True)
    b.add(anon_ws, is_admin=False)
    asyncio.run(b.send("tx_update",
                       {"tx": {"id": 1, "transcript": "hi", "voter": {"nodes": []}}}))
    admin_msg = json.loads(admin_ws.sent[0])
    anon_msg = json.loads(anon_ws.sent[0])
    assert "voter" in admin_msg["tx"]
    assert "voter" not in anon_msg["tx"]            # stripped for anon
    assert anon_msg["tx"]["transcript"] == "hi"     # non-sensitive survives


def test_presence_only_broadcast_to_super_admin():
    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send_text(self, m):
            self.sent.append(m)

    b = Broadcaster()
    admin_ws, user_ws, anon_ws = FakeWS(), FakeWS(), FakeWS()
    b.add(admin_ws, is_admin=True, username="a", can_settings=True)   # super/admin
    b.add(user_ws, is_admin=True, username="u", can_settings=False)   # plain user
    b.add(anon_ws, is_admin=False)                                    # anonymous
    asyncio.run(b.broadcast_presence())
    assert len(admin_ws.sent) == 1
    msg = json.loads(admin_ws.sent[0])
    assert msg["type"] == "presence" and msg["count"] == 3  # counts everyone
    # a plain signed-in user and an anon visitor never receive the count
    assert user_ws.sent == []
    assert anon_ws.sent == []


# ---- security headers ----

def test_security_headers_on_index(client):
    r = client.get("/")
    csp = r.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_csp_img_src_allows_the_map_tile_host(client):
    """Whatever tile host GeoMap.tsx uses MUST be whitelisted in the CSP
    img-src, or the browser blocks every tile and the map renders blank.
    Guards the CARTO->Esri class of regression (tile host changed in the
    component but not in the CSP)."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "web/src/components/GeoMap.tsx").read_text(encoding="utf-8")
    hosts = set(re.findall(
        r"https://([A-Za-z0-9.-]+)/[A-Za-z0-9./{}$_-]*(?:MapServer/tile|\{z\})", src))
    assert hosts, "no tile URL found in GeoMap.tsx — update this guard"
    csp = client.get("/").headers["content-security-policy"]
    img = next(d for d in csp.split(";") if d.strip().startswith("img-src"))
    for h in hosts:
        assert h in img, f"map tile host {h!r} not in CSP img-src: {img.strip()!r}"


def test_logo_keeps_its_own_sandbox_csp(client):
    login(client)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    client.post("/api/settings/logo",
                files={"file": ("logo.png", png, "image/png")})
    r = client.get("/api/logo")
    assert r.status_code == 200
    # the global middleware must not clobber the stricter per-endpoint policy
    assert r.headers["content-security-policy"] == "sandbox"
