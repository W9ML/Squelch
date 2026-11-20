# Changelog

## 2.0.0 — security hardening

A security release. It tightens the defaults, adds the access controls a
private deployment needs, and moves the frontend off an end-of-life framework. **Read "Upgrading from 1.x" before you
update — a few defaults changed on purpose.**

### Fixed

- **No default credentials.** The example configuration no longer ships an
  administrator password, and a weak or well-known one is refused rather than
  used. The first account is created by the setup wizard, which now asks for a
  one-time **setup code** that only the server knows
  (`/var/lib/squelch/setup-code`, also in the service log).
- **Login rate limiting uses the real client address.** Forwarded-address
  headers are honoured only from the reverse proxies listed in
  `[web] trusted_proxies` (loopback by default).
- **The node reporter endpoints require the shared token.** `/api/mdc`,
  `/api/source` and `/api/link` refuse requests unless `[mdc] forward_token` is
  configured and matches. The installer generates one.

### Added

- **Private mode** — `[web] require_login = true` serves the feed, recordings,
  logbook, speakers, statistics and live audio only to logged-in accounts.
  Anonymous visitors get a sign-in page and nothing else. Off by default.
- **Two-factor sign-in (TOTP)** — any account can enrol an authenticator app;
  `[web] require_2fa = true` makes it mandatory. A super admin can reset a lost
  authenticator, and `python -m squelch --reset-2fa <user>` recovers a locked-out
  sole administrator.
- **Revocable sessions** — sessions are random server-side tokens (stored as
  hashes) with a 7-day lifetime and 24-hour idle timeout, both configurable.
  Logout, a password change, deleting the account, or a super admin ends them
  immediately. Settings shows where you're signed in, with "sign out everywhere
  else".
- **Audit log** — sign-ins (and failures), account changes, and every delete,
  purge, voiceprint reset and settings change are recorded with who did it and
  from which address. Super admins read it in Settings or at `/api/audit`.
- **HTTPS options** — `[web] force_https` (redirect + HSTS behind a TLS proxy)
  and `[web] tls_cert` / `tls_key` (serve HTTPS directly). A startup warning
  names a plain-HTTP, non-loopback bind.
- **Audio sender allow-list** — `[usrp] allow_from` drops audio frames from any
  address but the node's.
- `SECURITY.md`, `CONTRIBUTING.md`, a "Security & deployment" guide in the
  README, CI (security tests, frontend build, dependency audits) and Dependabot.

### Changed

- **Roles are now least-privilege.** A `user` account is view-only: it can see
  login-gated views and manage its own alerts, password and two-factor, and
  nothing else. Deleting, purging and reprocessing recordings, naming or merging
  speakers, resetting voiceprints, linking MDC units and Time Machine exports
  now need `admin` or `super`. (In 1.x any logged-in account could do these.)
  New accounts default to `user`.
- Passwords: 12-character minimum (configurable up, never below 8), known
  defaults rejected.
- **Dependencies pinned.** `requirements.txt` holds tested ranges and the new
  `requirements.lock` pins every package with hashes; the installer uses it with
  `pip --require-hashes`.
- **Frontend moved from Next.js 14 (end-of-life) to Next.js 16 / React 19.**
  `npm audit` is clean.
- API responses are `Cache-Control: no-store`; the systemd unit drops all
  capabilities and tightens the sandbox; the config and data directory
  permissions are tightened by the installer.
- `/api/status` reports the real version (it said `0.1.0` through every 1.x
  release), and adds `logged_in`, `role`, `login_required`, `has_2fa`.
  `is_admin` now means "admin or super" — use `logged_in` for "has a session".

### Upgrading from 1.x

1. **Reporters need a token.** Before upgrading, set `[mdc] forward_token` in
   `squelch.toml` and give the same value to `mdc_forward.py` /
   `source_monitor.py` on the node (`SQUELCH_MDC_TOKEN`). Without it their
   reports are refused and origin badges / forwarded MDC IDs stop.
2. **Everyone is signed out once.** Old session cookies are not accepted.
3. **Check your `user` accounts.** Anyone who was moderating with a `user`
   role needs to be promoted to `admin`.
4. **If your `admin` account still has the password from the old example
   configuration, change it now.** 2.0 no longer creates an account from it,
   but it does not alter an account 1.x already made.
5. **Behind a proxy on another host?** Add its address to
   `[web] trusted_proxies`, or every visitor will appear as the proxy.
6. Rebuild the frontend (`install.sh` does) and restart the service — the
   Content-Security-Policy hashes are computed at startup.

Not included: single sign-on (put an authenticating proxy in front), and
pinning of the Whisper model files fetched from Hugging Face.
