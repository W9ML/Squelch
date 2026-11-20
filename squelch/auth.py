"""User accounts, login sessions and two-factor codes.

Users live in the database (scrypt-hashed passwords). The first account is
created in the browser by the first-run setup wizard, which asks for a
one-time setup code that only someone with access to the server can read.
A config `admin_password` can still bootstrap an "admin" account on an empty
database for headless installs — but never from a known-default value.

Sessions are random tokens kept server-side (only their SHA-256 is stored),
so a logout, a password change or an administrator can revoke them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import struct
import time
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

SESSION_COOKIE = "squelch_session"
SESSION_TTL = 7 * 24 * 3600          # default absolute lifetime (config)
SESSION_IDLE = 24 * 3600             # default idle timeout (config)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
MIN_PASSWORD_LEN = 12                # default; [web] min_password_len

ROLES = ("super", "admin", "user")

# values a password must never be, however long the minimum is
_KNOWN_DEFAULTS = {"admin", "password", "changeme", "squelch", "admin123",
                   "password123", "letmein", "testpw", "changeme123",
                   "administrator", "passwordpassword", "squelchsquelch",
                   "123456789012", "qwertyuiopas"}

# setup codes avoid look-alike characters (no 0/O, 1/I/L)
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SETUP_CODE_FILE = "setup-code"

TOTP_STEP = 30
TOTP_DIGITS = 6


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


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---- TOTP (RFC 6238, SHA-1, 30 s, 6 digits — what authenticator apps use) ----

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def totp_code(secret: str, step: int) -> str:
    key = base64.b32decode(secret, casefold=True)
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    num = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return f"{num % 10 ** TOTP_DIGITS:0{TOTP_DIGITS}d}"


def totp_match(secret: str, code: str, now: float | None = None,
               after_step: int | None = None) -> int | None:
    """The time step `code` is valid for (current, or one either side for
    clock drift), or None. Steps at or before `after_step` are refused so a
    code can't be used twice."""
    code = re.sub(r"\s+", "", code or "")
    if not (code.isdigit() and len(code) == TOTP_DIGITS):
        return None
    current = int((now if now is not None else time.time()) // TOTP_STEP)
    try:
        for step in (current, current - 1, current + 1):
            if after_step is not None and step <= after_step:
                continue
            if hmac.compare_digest(totp_code(secret, step), code):
                return step
    except (ValueError, TypeError):      # malformed stored secret
        return None
    return None


def totp_uri(secret: str, username: str, issuer: str = "Squelch") -> str:
    label = quote(f"{issuer}:{username}")
    return (f"otpauth://totp/{label}?secret={secret}"
            f"&issuer={quote(issuer)}&algorithm=SHA1"
            f"&digits={TOTP_DIGITS}&period={TOTP_STEP}")


class AuthManager:
    def __init__(self, db, data_dir: Path, bootstrap_password: str = "",
                 *, session_ttl: float = SESSION_TTL,
                 session_idle: float = SESSION_IDLE,
                 min_password_len: int = MIN_PASSWORD_LEN):
        self.db = db
        self.session_ttl = float(session_ttl)
        self.session_idle = float(session_idle)
        self.min_password_len = int(min_password_len)
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._setup_code: str | None = None

        if bootstrap_password and self.db.count_users() == 0:
            problem = self.password_problem(bootstrap_password, "admin")
            if problem:
                # never mint an account from a guessable config value — the
                # setup wizard (with its server-side code) is the way in
                log.error("SECURITY: the configured admin_password was NOT "
                          "used (%s). Finish setup in the browser with the "
                          "setup code, or set a real admin_password.", problem)
            else:
                self.db.create_user("admin",
                                    hash_password(bootstrap_password),
                                    role="super")
                log.warning("created initial 'admin' account from the "
                            "configured admin_password — remove it from the "
                            "config file now; it is never read again")
        self._init_setup_code()
        self.db.prune_sessions(time.time(), time.time() - self.session_idle)

    # ---- first-run setup code ----

    def _init_setup_code(self) -> None:
        path = self._data_dir / SETUP_CODE_FILE
        if self.db.count_users() > 0:
            path.unlink(missing_ok=True)
            return
        code = ""
        try:
            code = path.read_text().strip()
        except OSError:
            pass
        if len(code.replace("-", "")) != 8:
            raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
            code = f"{raw[:4]}-{raw[4:]}"
            try:
                path.write_text(code + "\n")
                path.chmod(0o600)
            except OSError as e:
                log.warning("could not write %s: %s", path, e)
        self._setup_code = code
        log.warning("no accounts yet — open the site and finish setup with "
                    "this one-time setup code: %s  (also in %s)", code, path)

    def check_setup_code(self, code: str) -> bool:
        if not self._setup_code:
            return False
        norm = lambda c: re.sub(r"[^A-Z0-9]", "", (c or "").upper())  # noqa: E731
        return hmac.compare_digest(norm(code), norm(self._setup_code))

    def setup_done(self) -> None:
        self._setup_code = None
        (self._data_dir / SETUP_CODE_FILE).unlink(missing_ok=True)

    # ---- accounts ----

    @property
    def enabled(self) -> bool:
        return self.db.count_users() > 0

    def password_problem(self, password: str, username: str = "") -> str | None:
        """Why `password` isn't acceptable, or None if it is."""
        if len(password) < self.min_password_len:
            return (f"password must be at least "
                    f"{self.min_password_len} characters")
        low = password.lower()
        if low in _KNOWN_DEFAULTS or (username and low == username.lower()):
            return "that password is a known default — choose another"
        if len(set(password)) < 4:
            return "password is too repetitive"
        return None

    def login(self, username: str, password: str) -> bool:
        stored = self.db.get_user_hash(username)
        if stored is None:
            # burn comparable time so usernames aren't probeable
            verify_password(password, hash_password("x"))
            return False
        return verify_password(password, stored)

    def role_of(self, username: str) -> str:
        # an unknown / missing role gets the LEAST privilege, never the most
        role = self.db.get_user_role(username)
        return role if role in ROLES else "user"

    def is_super(self, username: str | None) -> bool:
        return bool(username) and self.role_of(username) == "super"

    def is_admin(self, username: str | None) -> bool:
        """super and admin run the site (settings, moderation, cases);
        plain 'user' accounts only view and manage their own alerts."""
        return bool(username) and self.role_of(username) in ("super", "admin")

    # kept for callers that still use the older name
    can_settings = is_admin

    def change_password(self, username: str, current: str, new: str) -> None:
        """Raises ValueError with a user-facing message on failure."""
        if not self.login(username, current):
            raise ValueError("current password is wrong")
        problem = self.password_problem(new, username)
        if problem:
            raise ValueError("new " + problem)
        self.db.update_user_password(username, hash_password(new))

    def create_user(self, username: str, password: str,
                    role: str = "user") -> None:
        """Raises ValueError with a user-facing message on failure."""
        if role not in ROLES:
            raise ValueError("role must be 'super', 'admin', or 'user'")
        if not USERNAME_RE.match(username):
            raise ValueError("username must be 2-32 characters: "
                             "letters, digits, _ . -")
        problem = self.password_problem(password, username)
        if problem:
            raise ValueError(problem)
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
        if role not in ROLES:
            raise ValueError("role must be 'super', 'admin', or 'user'")
        if self.db.get_user_hash(username) is None:
            raise ValueError("no such user")
        if (role != "super" and self.role_of(username) == "super"
                and self.db.count_supers() <= 1):
            raise ValueError("can't demote the last super admin")
        self.db.set_user_role(username, role)

    # ---- two-factor ----

    def has_2fa(self, username: str | None) -> bool:
        if not username:
            return False
        row = self.db.get_user_totp(username)
        return bool(row and row["totp_secret"])

    def begin_2fa(self, username: str, issuer: str = "Squelch") -> dict:
        """Start enrolment: a fresh secret that only becomes active once
        confirm_2fa proves the authenticator app has it."""
        secret = new_totp_secret()
        self.db.set_user_totp_pending(username, secret)
        return {"secret": secret, "uri": totp_uri(secret, username, issuer)}

    def confirm_2fa(self, username: str, code: str) -> None:
        row = self.db.get_user_totp(username)
        pending = row and row["totp_pending"]
        if not pending:
            raise ValueError("start two-factor setup first")
        step = totp_match(pending, code)
        if step is None:
            raise ValueError("that code isn't right — check the app's clock "
                             "and try the next code")
        self.db.set_user_totp(username, secret=pending, pending=None,
                              last_step=step)

    def check_2fa(self, username: str, code: str) -> bool:
        """Verify a login code; each code works once."""
        row = self.db.get_user_totp(username)
        if not row or not row["totp_secret"]:
            return True
        step = totp_match(row["totp_secret"], code,
                          after_step=row["totp_last_step"])
        if step is None:
            return False
        self.db.set_user_totp_step(username, step)
        return True

    def clear_2fa(self, username: str) -> None:
        self.db.set_user_totp(username, secret=None, pending=None,
                              last_step=None)

    # ---- sessions ----

    def issue_token(self, username: str, ip: str | None = None,
                    user_agent: str | None = None) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.create_session(_token_hash(token), username, now,
                               now + self.session_ttl, ip, user_agent)
        return token

    def verify_token(self, token: str | None) -> str | None:
        """Returns the username for a live session, else None."""
        if not token or len(token) > 200:
            return None
        th = _token_hash(token)
        sess = self.db.get_session(th)     # joins users: dies with the account
        if sess is None:
            return None
        now = time.time()
        if sess["expires_at"] <= now or now - sess["last_seen"] > self.session_idle:
            self.db.delete_session(th)
            return None
        if now - sess["last_seen"] > 60:   # don't write on every request
            self.db.touch_session(sess["id"], now)
        return sess["username"]

    def revoke_token(self, token: str | None) -> None:
        if token:
            self.db.delete_session(_token_hash(token))

    def revoke_user(self, username: str, keep_token: str | None = None) -> int:
        """End a user's sessions (all, or all but the one given)."""
        return self.db.delete_user_sessions(
            username, _token_hash(keep_token) if keep_token else None)

    def list_sessions(self, username: str,
                      current_token: str | None = None) -> list[dict]:
        cur = _token_hash(current_token) if current_token else None
        out = []
        for s in self.db.list_sessions(username):
            th = s.pop("token_hash")
            s["current"] = th == cur
            out.append(s)
        return out
