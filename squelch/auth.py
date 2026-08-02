"""User accounts and admin sessions.

Users live in the database (scrypt-hashed passwords). On first start
with an empty users table, the config's admin_password bootstraps an
"admin" account; after that the config value is ignored and passwords
are managed from the web UI.

Sessions are HMAC-signed expiring tokens carrying the username. The
signing secret is generated once and kept in the data directory so
logins survive restarts.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
from pathlib import Path

log = logging.getLogger(__name__)

SESSION_COOKIE = "squelch_session"
SESSION_TTL = 30 * 24 * 3600  # 30 days

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
MIN_PASSWORD_LEN = 8

# obvious defaults a bootstrap password must not silently be
_WEAK_BOOTSTRAP = {"admin", "password", "changeme", "squelch", "squelch",
                   "admin123", "password123", "letmein", "testpw"}


def _bootstrap_is_weak(pw: str) -> bool:
    return len(pw) < MIN_PASSWORD_LEN or pw.lower() in _WEAK_BOOTSTRAP


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt,
                            n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(),
                                salt=bytes.fromhex(salt_hex),
                                n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


class AuthManager:
    def __init__(self, db, data_dir: Path, bootstrap_password: str = ""):
        self.db = db
        secret_file = data_dir / ".secret"
        data_dir.mkdir(parents=True, exist_ok=True)
        if secret_file.exists():
            self._secret = secret_file.read_bytes()
        else:
            self._secret = secrets.token_bytes(32)
            secret_file.write_bytes(self._secret)
            try:
                secret_file.chmod(0o600)
            except OSError:
                pass

        if bootstrap_password and self.db.count_users() == 0:
            # the first account is a super admin
            self.db.create_user("admin", hash_password(bootstrap_password),
                                role="super")
            log.warning("created initial 'admin' account from the configured "
                        "admin_password — change it after first login")
            if _bootstrap_is_weak(bootstrap_password):
                log.warning("SECURITY: the bootstrap admin password is weak, "
                            "short, or a known default. Anyone deploying this "
                            "config shares it — change it immediately.")

    @property
    def enabled(self) -> bool:
        return self.db.count_users() > 0

    def login(self, username: str, password: str) -> bool:
        stored = self.db.get_user_hash(username)
        if stored is None:
            # burn comparable time so usernames aren't probeable
            verify_password(password, hash_password("x"))
            return False
        return verify_password(password, stored)

    def role_of(self, username: str) -> str:
        return self.db.get_user_role(username) or "admin"

    def is_super(self, username: str | None) -> bool:
        return bool(username) and self.role_of(username) == "super"

    def can_settings(self, username: str | None) -> bool:
        """super and admin may change site settings; plain users may not."""
        return bool(username) and self.role_of(username) in ("super", "admin")

    def change_password(self, username: str, current: str, new: str) -> None:
        """Raises ValueError with a user-facing message on failure."""
        if not self.login(username, current):
            raise ValueError("current password is wrong")
        if len(new) < MIN_PASSWORD_LEN:
            raise ValueError(f"new password must be at least "
                             f"{MIN_PASSWORD_LEN} characters")
        self.db.update_user_password(username, hash_password(new))

    def create_user(self, username: str, password: str,
                    role: str = "admin") -> None:
        """Raises ValueError with a user-facing message on failure."""
        if role not in ("super", "admin", "user"):
            raise ValueError("role must be 'super', 'admin', or 'user'")
        if not USERNAME_RE.match(username):
            raise ValueError("username must be 2-32 characters: "
                             "letters, digits, _ . -")
        if len(password) < MIN_PASSWORD_LEN:
            raise ValueError(f"password must be at least "
                             f"{MIN_PASSWORD_LEN} characters")
        if self.db.get_user_hash(username) is not None:
            raise ValueError("that username already exists")
        self.db.create_user(username, hash_password(password), role)

    def delete_user(self, username: str, acting_user: str) -> None:
        """Raises ValueError with a user-facing message on failure."""
        if username == acting_user:
            raise ValueError("you can't delete your own account")
        if self.db.count_users() <= 1:
            raise ValueError("can't delete the last user")
        if (self.role_of(username) == "super"
                and self.db.count_supers() <= 1):
            raise ValueError("can't delete the last super admin")
        if not self.db.delete_user(username):
            raise ValueError("no such user")

    def set_role(self, username: str, role: str) -> None:
        """Promote/demote a user. Raises ValueError on failure."""
        if role not in ("super", "admin", "user"):
            raise ValueError("role must be 'super', 'admin', or 'user'")
        if self.db.get_user_hash(username) is None:
            raise ValueError("no such user")
        if (role == "admin" and self.role_of(username) == "super"
                and self.db.count_supers() <= 1):
            raise ValueError("can't demote the last super admin")
        self.db.set_user_role(username, role)

    # ---- tokens ----

    def issue_token(self, username: str) -> str:
        expires = int(time.time()) + SESSION_TTL
        payload = f"u:{username}:{expires}"
        sig = hmac.new(self._secret, payload.encode(),
                       hashlib.sha256).hexdigest()
        return f"{payload}:{sig}"

    def verify_token(self, token: str | None) -> str | None:
        """Returns the username for a valid session, else None."""
        if not token:
            return None
        parts = token.split(":")
        if len(parts) != 4 or parts[0] != "u":
            return None
        payload = ":".join(parts[:3])
        expect = hmac.new(self._secret, payload.encode(),
                          hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, parts[3]):
            return None
        try:
            if int(parts[2]) <= time.time():
                return None
        except ValueError:
            return None
        username = parts[1]
        # session dies with the account
        if self.db.get_user_hash(username) is None:
            return None
        return username
