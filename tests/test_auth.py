"""Auth: password hashing, user management, session tokens."""

import time

import pytest

from squelch.auth import (AuthManager, hash_password, verify_password)
from squelch.db import Database


def test_password_hash_roundtrip():
    h = hash_password("hunter2")
    assert h.startswith("scrypt$")
    assert verify_password("hunter2", h)
    assert not verify_password("wrong", h)


def test_hash_is_salted():
    assert hash_password("same") != hash_password("same")


def test_verify_rejects_garbage():
    assert not verify_password("x", "notahash")
    assert not verify_password("x", "")


def _auth(tmp_path, bootstrap="secret-admin-12"):
    db = Database(tmp_path / "t.db")
    return db, AuthManager(db, tmp_path, bootstrap)


def test_bootstrap_creates_admin(tmp_path):
    db, auth = _auth(tmp_path)
    assert db.count_users() == 1
    assert auth.login("admin", "secret-admin-12")
    assert not auth.login("admin", "nope")


def test_bootstrap_only_when_empty(tmp_path):
    db, auth = _auth(tmp_path)
    # second manager with a different password must NOT change existing users
    auth2 = AuthManager(db, tmp_path, "different")
    assert auth2.login("admin", "secret-admin-12")
    assert not auth2.login("admin", "different")


def test_create_and_delete_user(tmp_path):
    db, auth = _auth(tmp_path)
    auth.create_user("operator2", "password1234")
    assert auth.login("operator2", "password1234")
    auth.delete_user("operator2", acting_user="admin")
    assert db.get_user_hash("operator2") is None


def test_cannot_delete_self_or_last(tmp_path):
    db, auth = _auth(tmp_path)
    with pytest.raises(ValueError, match="own account"):
        auth.delete_user("admin", acting_user="admin")
    auth.create_user("bob", "password1234")
    auth.delete_user("bob", acting_user="admin")
    with pytest.raises(ValueError, match="last user"):
        # only admin left; deleting via another acting user still blocked
        auth.delete_user("admin", acting_user="someoneelse")


def test_create_user_validation(tmp_path):
    _db, auth = _auth(tmp_path)
    with pytest.raises(ValueError, match="username"):
        auth.create_user("a b", "password1234")
    with pytest.raises(ValueError, match="at least"):
        auth.create_user("valid", "short")
    auth.create_user("valid", "password1234")
    with pytest.raises(ValueError, match="already exists"):
        auth.create_user("valid", "password1234")


def test_change_password(tmp_path):
    _db, auth = _auth(tmp_path)
    with pytest.raises(ValueError, match="current password"):
        auth.change_password("admin", "wrong", "newpassword12")
    with pytest.raises(ValueError, match="at least"):
        auth.change_password("admin", "secret-admin-12", "abc")
    auth.change_password("admin", "secret-admin-12", "newpassword12")
    assert auth.login("admin", "newpassword12")
    assert not auth.login("admin", "secret-admin-12")


def test_token_roundtrip(tmp_path):
    _db, auth = _auth(tmp_path)
    tok = auth.issue_token("admin")
    assert auth.verify_token(tok) == "admin"
    assert auth.verify_token("garbage") is None
    assert auth.verify_token(None) is None


def test_token_dies_with_account(tmp_path):
    _db, auth = _auth(tmp_path)
    auth.create_user("temp", "password1234")
    tok = auth.issue_token("temp")
    assert auth.verify_token(tok) == "temp"
    auth.delete_user("temp", acting_user="admin")
    assert auth.verify_token(tok) is None


def test_tampered_token_rejected(tmp_path):
    _db, auth = _auth(tmp_path)
    tok = auth.issue_token("admin")
    # sessions are random server-side tokens: nothing derived from one, and
    # nothing in the old signed "u:name:expiry:sig" format, is ever accepted
    assert auth.verify_token(tok[:-1] + ("A" if tok[-1] != "A" else "B")) is None
    assert auth.verify_token(f"u:admin:{int(time.time()) + 999}:deadbeef") is None
    assert auth.verify_token("x" * 5000) is None


# ---- roles ----

def test_bootstrap_admin_is_super(tmp_path):
    _db, auth = _auth(tmp_path)
    assert auth.role_of("admin") == "super"
    assert auth.is_super("admin") is True


def test_new_users_default_to_least_privilege(tmp_path):
    _db, auth = _auth(tmp_path)
    auth.create_user("w9ml", "password1234")
    assert auth.role_of("w9ml") == "user"
    assert auth.is_admin("w9ml") is False
    assert auth.is_super("w9ml") is False
    auth.create_user("boss", "password1234", role="super")
    assert auth.is_super("boss") is True


def test_promote_demote(tmp_path):
    _db, auth = _auth(tmp_path)
    auth.create_user("operator2", "password1234")
    auth.set_role("operator2", "super")
    assert auth.is_super("operator2")
    auth.set_role("operator2", "admin")
    assert not auth.is_super("operator2")


def test_cannot_demote_or_delete_last_super(tmp_path):
    _db, auth = _auth(tmp_path)                # only 'admin' is super
    auth.create_user("bob", "password1234")       # a regular admin
    with pytest.raises(ValueError, match="last super"):
        auth.set_role("admin", "admin")
    with pytest.raises(ValueError, match="last super"):
        auth.delete_user("admin", acting_user="bob")
    # once a second super exists, demoting the first is allowed
    auth.set_role("bob", "super")
    auth.set_role("admin", "admin")
    assert not auth.is_super("admin")


def test_can_delete_default_admin_from_another_super(tmp_path):
    # the bootstrap 'admin' account has no special protection: another
    # super admin can remove it, as long as it isn't the last super.
    _db, auth = _auth(tmp_path)                        # 'admin' is the only super
    auth.create_user("w9ml", "password1234", role="super")
    auth.delete_user("admin", acting_user="w9ml")      # allowed: w9ml stays super
    assert _db.get_user_hash("admin") is None
    assert auth.is_super("w9ml")


def test_user_role_cannot_access_settings(tmp_path):
    _db, auth = _auth(tmp_path)
    auth.create_user("viewer", "password1234", role="user")
    assert auth.role_of("viewer") == "user"
    assert auth.is_super("viewer") is False
    assert auth.can_settings("viewer") is False        # the whole point
    # super and admin keep settings access
    assert auth.can_settings("admin") is True
    auth.create_user("mod", "password1234", role="admin")
    assert auth.can_settings("mod") is True
    assert auth.can_settings(None) is False
