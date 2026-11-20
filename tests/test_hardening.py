"""Security hardening (v2.0): default credentials, forwarded-address trust,
reporter token, private mode, roles, sessions, two-factor, TLS, audit log.

Each test here pins a behaviour an attacker would otherwise exploit; if one
fails, fix the code, not the test.
"""

import time
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from squelch import usrp
from squelch.auth import AuthManager, totp_code, totp_match
from squelch.config import Config, load_config
from squelch.db import Database
from squelch.web import ProxyTrust, create_app

ROOT = Path(__file__).resolve().parent.parent
PW = "correct-horse-battery"
TOKEN = {"x-mdc-token": "reporter-token-for-tests"}


def _cfg(tmp_path, **over):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.admin_password = PW
    cfg.mdc_forward_token = TOKEN["x-mdc-token"]
    cfg.usrp_port = 0
    cfg.transcribe_enabled = False
    cfg.speaker_id_enabled = False
    cfg.retention_days = 0
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _client(tmp_path, peer="203.0.113.50", **over):
    """A client whose socket address is `peer` (a direct internet visitor by
    default; pass 127.0.0.1 to play a local reverse proxy)."""
    return TestClient(create_app(_cfg(tmp_path, **over)), client=(peer, 40000))


def _login(c, user="admin", pw=PW, **kw):
    r = c.post("/api/login", json={"username": user, "password": pw}, **kw)
    assert r.status_code == 200, r.text
    return r


def _code(secret, offset=0):
    return totp_code(secret, int(time.time() // 30) + offset)


# ---- 1. no default credentials ----

def test_example_config_ships_no_admin_password():
    raw = tomllib.loads((ROOT / "config.example.toml").read_text())
    assert raw["web"].get("admin_password", "") == ""
    assert raw["mdc"].get("forward_token", "") == ""
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.admin_password == ""


@pytest.mark.parametrize("pw", ["changeme", "admin", "password", "short",
                                "aaaaaaaaaaaaaaaa", "squelchsquelch"])
def test_weak_bootstrap_password_creates_no_account(tmp_path, pw):
    db = Database(tmp_path / "t.db")
    auth = AuthManager(db, tmp_path, pw)
    assert db.count_users() == 0            # nothing to log in to
    assert not auth.login("admin", pw)
    assert auth.enabled is False            # setup wizard still required


def test_fresh_install_cannot_be_claimed_without_the_setup_code(tmp_path):
    with _client(tmp_path, admin_password="changeme") as c:
        assert c.get("/api/status").json()["needs_setup"] is True
        r = c.post("/api/login", json={"username": "admin", "password": "changeme"})
        assert r.status_code == 403
        body = {"username": "intruder", "password": PW}
        assert c.post("/api/setup", json=body).status_code == 403
        assert c.app.state.db.count_users() == 0


def test_setup_code_guessing_is_throttled(tmp_path):
    with _client(tmp_path, admin_password="") as c:
        codes = [c.post("/api/setup", json={"username": "x1", "password": PW,
                                            "setup_code": f"AAAA-AA{i:02d}"}
                        ).status_code for i in range(8)]
        assert codes[:5] == [403] * 5 and 429 in codes[5:]


def test_password_policy(tmp_path):
    with _client(tmp_path) as c:
        _login(c)
        for bad in ("short", "elevenchars", "changeme", "newuser1"):
            r = c.post("/api/users", json={"username": "newuser1",
                                           "password": bad, "role": "user"})
            assert r.status_code == 400, bad
        assert c.get("/api/status").json()["min_password_len"] == 12


# ---- 2. forwarded-address trust / login throttle ----

def test_proxytrust_ignores_forged_headers_from_direct_clients():
    class C:                                   # stand-in for request.client
        def __init__(self, host): self.host = host
    pt = ProxyTrust(["127.0.0.1", "::1"])
    forged = {"x-forwarded-for": "1.2.3.4", "x-forwarded-proto": "https"}
    assert pt.client_ip(forged, C("203.0.113.9")) == "203.0.113.9"
    assert pt.is_https(forged, C("203.0.113.9"), "http") is False
    # via the local proxy: the RIGHT-most address is the one the proxy saw;
    # anything to its left is client-supplied and must be ignored
    via = {"x-forwarded-for": "1.2.3.4, 198.51.100.7", "x-forwarded-proto": "https"}
    assert pt.client_ip(via, C("127.0.0.1")) == "198.51.100.7"
    assert pt.is_https(via, C("127.0.0.1"), "http") is True
    # chained trusted proxies are skipped from the right
    pt2 = ProxyTrust(["127.0.0.1", "10.0.0.0/8"])
    chain = {"x-forwarded-for": "6.6.6.6, 198.51.100.7, 10.1.2.3"}
    assert pt2.client_ip(chain, C("127.0.0.1")) == "198.51.100.7"
    # garbage in the chain falls back to the socket peer
    assert pt.client_ip({"x-forwarded-for": "not-an-ip"}, C("127.0.0.1")) == "127.0.0.1"
    assert pt.client_ip({}, None) is None


def test_login_throttle_cannot_be_dodged_with_x_forwarded_for(tmp_path):
    with _client(tmp_path) as c:               # direct client
        codes = [c.post("/api/login",
                        json={"username": "admin", "password": f"wrong-{i}"},
                        headers={"X-Forwarded-For": f"198.51.100.{i}"}
                        ).status_code for i in range(12)]
        assert codes[:5] == [403] * 5
        assert set(codes[5:]) == {429}


def test_login_throttle_behind_proxy_keys_on_the_proxy_reported_address(tmp_path):
    with _client(tmp_path, peer="127.0.0.1") as c:   # e.g. cloudflared
        def attempt(left, real):
            return c.post("/api/login",
                          json={"username": "admin", "password": "wrong-pass"},
                          headers={"X-Forwarded-For": f"{left}, {real}"}).status_code
        # an attacker rotating the part of the header they control gets nowhere
        codes = [attempt(f"10.9.8.{i}", "198.51.100.66") for i in range(8)]
        assert codes[:5] == [403] * 5 and set(codes[5:]) == {429}
        # and has not locked out a different real visitor
        assert attempt("10.9.8.1", "198.51.100.99") == 403


# ---- 3. reporter endpoints ----

REPORTS = (("/api/source", {"source": "node", "node": "99999"}),
           ("/api/mdc", {"unit": "1234"}),
           ("/api/link", {"connected": ["1"]}))


def test_reporter_endpoints_are_closed_without_a_configured_token(tmp_path):
    with _client(tmp_path, mdc_forward_token="") as c:
        for path, body in REPORTS:
            assert c.post(path, json=body).status_code == 403, path
            # sending an empty or made-up token doesn't open them either
            assert c.post(path, json=body,
                          headers={"x-mdc-token": ""}).status_code == 403
            assert c.post(path, json=body,
                          headers={"x-mdc-token": "anything"}).status_code == 403
        # ...and not even to a logged-in super admin (it's a machine channel)
        _login(c)
        assert c.post("/api/source", json=REPORTS[0][1]).status_code == 403


def test_reporter_endpoints_need_the_right_token(tmp_path):
    with _client(tmp_path) as c:
        for path, body in REPORTS:
            assert c.post(path, json=body).status_code == 403
            assert c.post(path, json=body,
                          headers={"x-mdc-token": "wrong"}).status_code == 403
            assert c.post(path, json=body, headers=TOKEN).status_code == 200


# ---- 4. private mode ----

PUBLIC_READS = ("/api/transmissions", "/api/facets", "/api/stats",
                "/api/callsigns", "/api/avatars", "/api/speakers",
                "/api/network", "/api/mdc_units", "/api/transmissions/1",
                "/api/transmissions/1/audio", "/api/transmissions/1/similar",
                "/api/speakers/1", "/api/nodes/1", "/api/callsign/W9ML")


def test_public_mode_is_unchanged_by_default(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/api/status").json()["login_required"] is False
        for path in ("/api/transmissions", "/api/stats", "/api/speakers"):
            assert c.get(path).status_code == 200, path
        with c.websocket_connect("/ws"):
            pass


def test_private_mode_serves_nothing_without_a_login(tmp_path):
    with _client(tmp_path, require_login=True) as c:
        db = c.app.state.db
        tx = db.insert_transmission(time.time() - 5, time.time() - 3, 2000)
        db.update_transmission(tx, transcript="net traffic", status="done")
        for path in PUBLIC_READS:
            r = c.get(path)
            assert r.status_code == 401, (path, r.status_code)
            assert "net traffic" not in r.text
        # the login screen gets branding and nothing else
        s = c.get("/api/status").json()
        assert s["login_required"] is True and s["logged_in"] is False
        for leaked in ("node_number", "callsign", "version", "usrp_port",
                       "whisper_model", "queue_depth"):
            assert leaked not in s, leaked
        # both live sockets refuse the handshake
        for ws_path in ("/ws", "/ws/audio"):
            with pytest.raises(Exception):
                with c.websocket_connect(ws_path):
                    pass
        # the node's reporters still work (token-checked, not session-checked)
        assert c.post("/api/source", json=REPORTS[0][1], headers=TOKEN).status_code == 200
        assert c.post("/api/source", json=REPORTS[0][1]).status_code == 403


def test_private_mode_opens_for_any_logged_in_account(tmp_path):
    with _client(tmp_path, require_login=True) as c:
        _login(c)
        c.post("/api/users", json={"username": "viewer", "password": PW + "-v",
                                   "role": "user"})
        c.post("/api/logout")
        assert c.get("/api/transmissions").status_code == 401
        _login(c, "viewer", PW + "-v")
        assert c.get("/api/transmissions").status_code == 200
        assert "node_number" in c.get("/api/status").json()
        with c.websocket_connect("/ws"):
            pass
        with c.websocket_connect("/ws/audio"):
            pass
        c.post("/api/logout")
        assert c.get("/api/transmissions").status_code == 401


# ---- 5. roles + audit trail ----

def test_missing_role_gets_least_privilege(tmp_path):
    db = Database(tmp_path / "t.db")
    auth = AuthManager(db, tmp_path, PW)
    auth.create_user("someone", PW + "-x", "admin")
    db.set_user_role("someone", "bogus")
    assert auth.role_of("someone") == "user"
    assert auth.is_admin("someone") is False


def test_new_accounts_default_to_user_and_bad_roles_are_refused(tmp_path):
    with _client(tmp_path) as c:
        _login(c)
        assert c.post("/api/users", json={"username": "nobody1",
                                          "password": PW + "-n"}).status_code == 200
        assert c.post("/api/users", json={"username": "nobody2", "password": PW + "-n",
                                          "role": "root"}).status_code == 400
        roles = {u["username"]: u["role"] for u in c.get("/api/users").json()["users"]}
        assert roles["nobody1"] == "user" and "nobody2" not in roles


def test_destructive_actions_are_audited_with_who_and_where(tmp_path):
    with _client(tmp_path, peer="127.0.0.1") as c:
        xff = {"X-Forwarded-For": "198.51.100.23"}
        c.post("/api/login", json={"username": "admin", "password": "wrong-pass"},
               headers=xff)
        _login(c, headers=xff)
        db = c.app.state.db
        tx = db.insert_transmission(time.time() - 2, time.time(), 2000)
        assert c.delete(f"/api/transmissions/{tx}", headers=xff).status_code == 200
        assert c.post("/api/transmissions/purge", headers=xff,
                      json={"start": 0, "end": time.time() + 9}).status_code == 200
        assert c.post("/api/speakers/reset_voiceprints", headers=xff).status_code == 200
        c.post("/api/users", headers=xff,
               json={"username": "temp1", "password": PW + "-t", "role": "admin"})
        entries = c.get("/api/audit").json()["entries"]
        by_action = {e["action"]: e for e in entries}
        for action in ("login_failed", "login", "transmission_deleted",
                       "transmissions_purged", "voiceprints_reset_all",
                       "user_created"):
            assert action in by_action, action
        assert by_action["transmissions_purged"]["username"] == "admin"
        assert by_action["transmissions_purged"]["ip"] == "198.51.100.23"
        assert by_action["login_failed"]["username"] is None
        # the trail is for super admins only
        c.post("/api/logout")
        _login(c, "temp1", PW + "-t", headers=xff)
        assert c.get("/api/audit").status_code == 403


# ---- 6. sessions ----

def test_logout_kills_the_session_server_side(tmp_path):
    with _client(tmp_path) as c:
        _login(c)
        stolen = c.cookies.get("squelch_session")
        assert c.post("/api/logout").status_code == 200
        c.cookies.set("squelch_session", stolen)        # a copied cookie
        assert c.get("/api/users").status_code == 403
        assert c.get("/api/status").json()["logged_in"] is False


def test_password_change_ends_every_other_session(tmp_path):
    with _client(tmp_path) as c:
        _login(c)
        old = c.cookies.get("squelch_session")
        c.cookies.clear()
        _login(c)                                       # second device
        assert len(c.get("/api/account/sessions").json()["sessions"]) == 2
        r = c.post("/api/password", json={"current": PW, "new": PW + "-new"})
        assert r.status_code == 200 and r.json()["sessions_ended"] == 1
        assert c.get("/api/users").status_code == 200   # this device stays in
        c.cookies.clear()
        c.cookies.set("squelch_session", old)
        assert c.get("/api/users").status_code == 403   # the other one is out


def test_session_is_a_random_token_stored_only_as_a_hash(tmp_path):
    with _client(tmp_path) as c:
        _login(c)
        tok = c.cookies.get("squelch_session")
        assert "admin" not in tok and len(tok) >= 40
        rows = c.app.state.db._conn.execute("SELECT * FROM sessions").fetchall()
        assert len(rows) == 1 and tok not in [str(v) for v in tuple(rows[0])]


def test_sessions_expire_absolutely_and_when_idle(tmp_path):
    db = Database(tmp_path / "t.db")
    auth = AuthManager(db, tmp_path, PW, session_ttl=3600, session_idle=600)
    tok = auth.issue_token("admin")
    assert auth.verify_token(tok) == "admin"
    with db._conn:                                      # unused for 11 minutes
        db._conn.execute("UPDATE sessions SET last_seen=?", (time.time() - 660,))
    assert auth.verify_token(tok) is None
    tok = auth.issue_token("admin")
    with db._conn:                                      # past its hard limit
        db._conn.execute("UPDATE sessions SET expires_at=?", (time.time() - 1,))
    assert auth.verify_token(tok) is None
    assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_super_can_end_another_users_sessions_and_delete_does_too(tmp_path):
    with _client(tmp_path) as c:
        _login(c)
        c.post("/api/users", json={"username": "op2", "password": PW + "-2",
                                   "role": "admin"})
        admin_tok = c.cookies.get("squelch_session")
        c.cookies.clear()
        _login(c, "op2", PW + "-2")
        op_tok = c.cookies.get("squelch_session")
        assert c.post("/api/users/admin/revoke_sessions").status_code == 403
        c.cookies.clear(); c.cookies.set("squelch_session", admin_tok)
        assert c.post("/api/users/op2/revoke_sessions").json()["ended"] == 1
        c.cookies.clear(); c.cookies.set("squelch_session", op_tok)
        assert c.get("/api/status").json()["logged_in"] is False


def test_cookie_flags(tmp_path):
    with _client(tmp_path, peer="127.0.0.1") as c:
        r = c.post("/api/login", json={"username": "admin", "password": PW},
                   headers={"X-Forwarded-For": "198.51.100.5",
                            "X-Forwarded-Proto": "https"})
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        assert "secure" in cookie
        assert f"max-age={7 * 86400}" in cookie
    with _client(tmp_path / "b") as c:          # direct client lying about TLS
        r = c.post("/api/login", json={"username": "admin", "password": PW},
                   headers={"X-Forwarded-Proto": "https"})
        assert "secure" not in r.headers["set-cookie"].lower()


# ---- 6b. two-factor ----

def test_totp_matches_the_rfc_6238_reference_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"      # "12345678901234567890"
    assert totp_code(secret, 59 // 30) == "287082"   # RFC 6238 Appendix B (6 digits)
    assert totp_match(secret, "287082", now=59) == 1
    assert totp_match(secret, "287082", now=59, after_step=1) is None   # replay
    assert totp_match(secret, "28708", now=59) is None
    assert totp_match(secret, "abcdef", now=59) is None


def test_two_factor_enrol_login_replay_and_reset(tmp_path):
    with _client(tmp_path) as c:
        _login(c)
        assert c.get("/api/account/2fa").json() == {"enabled": False, "required": False}
        begun = c.post("/api/account/2fa/begin").json()
        secret = begun["secret"]
        assert begun["uri"].startswith("otpauth://totp/")
        # not active until a code proves the app has the secret
        assert c.post("/api/account/2fa/confirm", json={"code": "000000"}).status_code == 400
        assert c.get("/api/status").json()["has_2fa"] is False
        assert c.post("/api/account/2fa/confirm",
                      json={"code": _code(secret, -1)}).status_code == 200
        assert c.get("/api/status").json()["has_2fa"] is True
        c.post("/api/logout")

        # password alone is no longer enough
        r = c.post("/api/login", json={"username": "admin", "password": PW})
        assert r.status_code == 401 and r.json()["totp_required"] is True
        assert c.get("/api/status").json()["logged_in"] is False
        assert c.post("/api/login", json={"username": "admin", "password": PW,
                                          "code": "000000"}).status_code == 403
        # the code used to enrol can't be replayed
        assert c.post("/api/login", json={"username": "admin", "password": PW,
                                          "code": _code(secret, -1)}).status_code == 403
        good = _code(secret)
        assert c.post("/api/login", json={"username": "admin", "password": PW,
                                          "code": good}).status_code == 200
        c.post("/api/logout")
        assert c.post("/api/login", json={"username": "admin", "password": PW,
                                          "code": good}).status_code == 403   # once only
        # a wrong password never reveals whether two-factor is on
        r = c.post("/api/login", json={"username": "admin", "password": "wrong-pass"})
        assert r.status_code == 403 and "totp_required" not in r.json()

        # turning it off needs the password
        c.post("/api/login", json={"username": "admin", "password": PW,
                                   "code": _code(secret, 1)})
        assert c.post("/api/account/2fa/disable",
                      json={"password": "wrong-pass"}).status_code == 403
        assert c.post("/api/account/2fa/disable", json={"password": PW}).status_code == 200
        assert c.get("/api/status").json()["has_2fa"] is False


def test_require_2fa_locks_an_unenrolled_account_to_enrolment(tmp_path):
    with _client(tmp_path, require_2fa=True, require_login=True) as c:
        _login(c)
        s = c.get("/api/status").json()
        assert s["logged_in"] is True and s["must_enroll_2fa"] is True
        assert s["is_admin"] is False and "node_number" not in s
        for path in ("/api/transmissions", "/api/users", "/api/watchlist"):
            assert c.get(path).status_code in (401, 403), path
        with pytest.raises(Exception):
            with c.websocket_connect("/ws"):
                pass
        secret = c.post("/api/account/2fa/begin").json()["secret"]
        assert c.post("/api/account/2fa/confirm",
                      json={"code": _code(secret)}).status_code == 200
        s = c.get("/api/status").json()
        assert s["must_enroll_2fa"] is False and s["is_super"] is True
        assert c.get("/api/transmissions").status_code == 200
        # required means it can't be switched off by the user
        assert c.post("/api/account/2fa/disable", json={"password": PW}).status_code == 400


def test_super_can_reset_a_lost_authenticator(tmp_path):
    with _client(tmp_path) as c:
        _login(c)
        c.post("/api/users", json={"username": "op2", "password": PW + "-2",
                                   "role": "user"})
        auth = c.app.state.auth
        secret = auth.begin_2fa("op2")["secret"]
        auth.confirm_2fa("op2", _code(secret))
        assert auth.has_2fa("op2")
        assert c.delete("/api/users/op2/2fa").status_code == 200
        assert not auth.has_2fa("op2")
        assert c.delete("/api/users/nobody/2fa").status_code == 404


# ---- 7. transport ----

def test_force_https_redirects_only_what_a_trusted_proxy_says_was_http(tmp_path):
    with _client(tmp_path, peer="127.0.0.1", force_https=True) as c:
        r = c.get("/api/status", headers={"X-Forwarded-Proto": "http",
                                          "Host": "squelch.example.org"},
                  follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == "https://squelch.example.org/api/status"
        r = c.get("/api/status", headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200
        assert "max-age=" in r.headers["strict-transport-security"]
        # direct LAN request through no proxy header: untouched
        assert c.get("/api/status").status_code == 200
    with _client(tmp_path / "b", force_https=True) as c:     # untrusted peer
        r = c.get("/api/status", headers={"X-Forwarded-Proto": "http"},
                  follow_redirects=False)
        assert r.status_code == 200
        assert "strict-transport-security" not in r.headers


def test_api_responses_are_not_cacheable(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/api/status").headers["cache-control"] == "no-store"
        assert c.get("/api/transmissions").headers["cache-control"] == "no-store"


def test_usrp_allow_from_drops_other_senders():
    class Seg:
        def __init__(self): self.frames = 0
        def feed(self, frame): self.frames += 1
    frame = b"USRP" + bytes(28) + bytes(320)
    assert usrp.parse_frame(frame) is not None
    seg = Seg()
    proto = usrp.USRPProtocol(seg, ["192.0.2.10", "10.20.0.0/16"])
    proto.datagram_received(frame, ("192.0.2.10", 34001))
    proto.datagram_received(frame, ("10.20.3.4", 34001))
    assert seg.frames == 2
    proto.datagram_received(frame, ("192.0.2.11", 34001))
    proto.datagram_received(frame, ("203.0.113.5", 34001))
    assert seg.frames == 2 and proto.dropped == 2
    open_seg = Seg()                                  # no list = accept all
    usrp.USRPProtocol(open_seg).datagram_received(frame, ("203.0.113.5", 1))
    assert open_seg.frames == 1


def test_version_is_reported_and_real(tmp_path):
    from squelch import __version__
    assert tuple(int(p) for p in __version__.split(".")) >= (2, 0, 0)
    with _client(tmp_path) as c:
        assert c.get("/api/status").json()["version"] == __version__
