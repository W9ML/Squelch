# Contributing

Squelch is GPL-2.0 — issues and pull requests are welcome.

## Ground rules

1. **Receive-only, always.** Squelch must never gain a code path that can key
   the node or send audio back to it. Changes that add one will not be merged.
2. **Security reports go private.** See [SECURITY.md](SECURITY.md) — don't file
   a public issue for a vulnerability.
3. **Keep the security tests green.** `tests/test_hardening.py`,
   `tests/test_security.py`, `tests/test_auth.py` and `tests/test_setup.py` pin
   down behaviour an attacker would otherwise exploit. If one fails, fix the
   code, not the test. New endpoints need an explicit permission check
   (`require_login`, `require_admin` or `require_super`) and, if they change or
   delete shared data, an `audit(...)` entry.

## Working on it

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
pytest

cd web && npm ci && npm run build      # the frontend (Next.js static export)
```

Restart the server after rebuilding the frontend: the Content-Security-Policy
hashes for its inline bootstrap scripts are computed at startup.

## Dependencies

- Python: add or change a direct dependency in `requirements.txt`, then
  regenerate `requirements.lock` (the command is in its header).
- Frontend: `npm install <pkg>` in `web/` and commit `package-lock.json`.

CI audits both sets (`pip-audit`, `npm audit`) on every push and weekly;
Dependabot opens a pull request when a pinned dependency has a security fix.
