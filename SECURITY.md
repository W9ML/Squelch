# Security policy

Squelch is a self-hosted, receive-only monitor for AllStarLink nodes. It holds
recordings and logins, and some people run it on the public internet, so
security reports are taken seriously.

## Supported versions

Security fixes go into the latest release line only.

| Version | Supported |
|---|---|
| 2.x | yes |
| 1.x and earlier | no — upgrade to 2.x (see the v2.0.0 release notes) |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private reporting instead: on this repository, open the
**Security** tab → **Report a vulnerability**. That creates a private advisory
only the maintainer can see.

Please include:

- the Squelch version (`/api/status` → `version`, or the release tag),
- how it is deployed (direct, behind nginx / Cloudflare Tunnel, private mode on
  or off),
- what an attacker can do, and the steps or a proof of concept.

What to expect:

- an acknowledgement within 7 days,
- an assessment, and a fix or a mitigation plan, within 30 days for anything
  exploitable,
- credit in the release notes if you want it.

This is a volunteer-maintained project with no bug bounty.

## Scope

In scope: anything in this repository — the web application and API, the
installer, the node-side reporter tools, and the default configuration.

Out of scope: vulnerabilities in a deployment's own reverse proxy, operating
system or network; attacks that need an existing super-admin login; and the
content of radio traffic itself (it is transmitted in the clear, over the air).

## Hardening guidance

See **Security & deployment** in the [README](README.md#security--deployment)
for the settings an operator is expected to choose: HTTPS, trusted proxies,
private mode, two-factor, the audio-port allow-list and the reporter token.
