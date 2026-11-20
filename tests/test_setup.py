"""First-run setup wizard: /api/setup + the needs_setup gate."""

import pytest
from fastapi.testclient import TestClient

from squelch.config import Config
from squelch.web import create_app


def _cfg(tmp_path, admin_password):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.admin_password = admin_password
    cfg.usrp_port = 0
    cfg.transcribe_enabled = False
    cfg.speaker_id_enabled = False
    cfg.retention_days = 0
    return cfg


@pytest.fixture
def fresh(tmp_path):
    """No bootstrapped admin -> the setup wizard is required."""
    with TestClient(create_app(_cfg(tmp_path, ""))) as c:
        yield c


@pytest.fixture
def seeded(tmp_path):
    """An admin bootstrapped from config -> setup is already complete."""
    with TestClient(create_app(_cfg(tmp_path, "bootstrap-pw-123"))) as c:
        yield c


def test_needs_setup_on_empty_db(fresh):
    s = fresh.get("/api/status").json()
    assert s["needs_setup"] is True
    assert s["is_admin"] is False


def _code(client):
    return client.app.state.auth._setup_code


def test_setup_needs_the_server_side_code(fresh):
    body = {"username": "chief", "password": "supersecret12"}
    assert fresh.post("/api/setup", json=body).status_code == 403
    assert fresh.post("/api/setup",
                      json={**body, "setup_code": "AAAA-AAAA"}).status_code == 403
    assert fresh.get("/api/status").json()["needs_setup"] is True
    # the code is on disk for the operator, readable only by the service user
    f = fresh.app.state.auth._data_dir / "setup-code"
    assert f.read_text().strip() == _code(fresh)
    # forgiving about case / dashes / spaces
    sloppy = _code(fresh).replace("-", " ").lower()
    assert fresh.post("/api/setup",
                      json={**body, "setup_code": sloppy}).status_code == 200
    assert not f.exists()                       # single use


def test_setup_creates_super_admin_and_self_closes(fresh):
    r = fresh.post("/api/setup", json={
        "setup_code": _code(fresh),
        "username": "chief", "password": "supersecret12",
        "callsign": "W9ML", "node": "12345", "footer": "de W9ML",
    })
    assert r.status_code == 200, r.text

    s = fresh.get("/api/status").json()
    assert s["needs_setup"] is False          # gate lifts
    assert s["logged_in"] is True             # logged straight in
    assert s["is_admin"] is True
    assert s["is_super"] is True              # as a super-admin
    assert s["username"] == "chief"
    assert s["brand_callsign"] == "W9ML"      # station identity applied
    assert s["brand_node"] == "12345"

    # self-closes now that a user exists — cannot be used to add accounts
    again = fresh.post("/api/setup", json={
        "username": "sneaky", "password": "anotherpass12"})
    assert again.status_code == 403


def test_setup_rejects_weak_password(fresh):
    r = fresh.post("/api/setup", json={"setup_code": _code(fresh),
                                       "username": "chief", "password": "short"})
    assert r.status_code == 400
    assert fresh.get("/api/status").json()["needs_setup"] is True   # nothing created


def test_setup_closed_when_admin_bootstrapped(seeded):
    assert seeded.get("/api/status").json()["needs_setup"] is False
    r = seeded.post("/api/setup", json={"username": "x", "password": "whatever12345"})
    assert r.status_code == 403
