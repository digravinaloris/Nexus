# Security Policy

Nexus is a hobby project maintained by a single developer. This document explains what's covered, how to report a vulnerability, and what security measures are already in place.

## Supported Versions

There are no version tags — only the code currently deployed from the `main` branch is supported. If you find an issue in an older commit that has since been changed, please check `main` first; it may already be fixed.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.** Publicly disclosing a bug before a fix is live puts every server running the bot at risk.

Instead:

1. Use GitHub's private vulnerability reporting: go to the repository's **Security** tab → **Report a vulnerability**, or
2. Contact the maintainer directly via Discord DM.

When reporting, please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce it (proof-of-concept code or requests are welcome)
- Any suggested fix, if you have one

This is a solo-maintained hobby project, not a company with a security team, so there's no guaranteed response SLA — but reports are taken seriously and looked at as soon as possible. Please give a reasonable amount of time for a fix before any public disclosure.

## Scope

**In scope:**
- The bot's Discord commands and event handlers (`main.py`)
- The Flask REST API used by the companion Android app
- The web dashboard (OAuth2 login, server configuration, permissions, tickets, audit log)
- Data stored in MongoDB (config, warnings, sanctions, notes, audit log)

**Out of scope:**
- Discord's own platform and infrastructure (report those to Discord directly)
- MongoDB Atlas and Render's infrastructure (report those to the respective providers)
- Vulnerabilities that require a compromised bot token, database credentials, or server-owner-level access to begin with — if you already have those, you have full access by design

## Security Measures Already in Place

This isn't a claim of being bulletproof — it's a list of what's actually implemented, so reports can focus on what might still be missing.

**Authentication & sessions**
- Dashboard login uses Discord OAuth2 with a signed, single-use `state` parameter to prevent CSRF on the login flow
- Session cookies are `Secure`, `HttpOnly`, and `SameSite=Lax`
- Access tokens are held server-side in the session only, never exposed to the client or logged

**Authorization**
- Every sensitive dashboard action re-checks the user's admin/owner status directly against Discord's API — never trusting a role or permission claimed by the client
- Server-owner-only actions (ticket setup, command-permission templates, API key regeneration, bot lockdown) are gated separately from admin-level actions, matching the same restriction enforced by the Discord commands themselves
- A server only appears on a user's dashboard if the bot is actually present there and the user is genuinely an Administrator of that server

**Input validation**
- Guild, channel, and role IDs submitted through any form are re-validated against the real objects of the *target* server before anything is written — a forged or cross-server ID is rejected rather than trusted
- Command-permission changes are checked against a fixed list of real bot commands
- Mongo writes use parameterized field updates (`$set`, `$addToSet`, `$pull`); user input is never interpolated directly into a query

**Secrets**
- Bot token, OAuth2 client secret, database URI, and Flask session secret are all read from environment variables — never hardcoded or committed
- Two previously exposed tokens were purged from the entire git history (not just reverted) and rotated

**Availability / abuse resistance**
- Login retries back off with increasing delays after a Discord/Cloudflare rate limit instead of crash-looping and making the situation worse
- Per-server API keys for the mobile app mean a leaked key only exposes one server, not the whole bot

**Accountability**
- Every dashboard change is written to an audit log (who, what, when), viewable by the server owner
- Dashboard changes can optionally also be posted to the server's own Discord logs channel

## Known Limitations

- Hosted on Render's free tier, which uses a shared outbound IP pool — not something this project controls
- No formal penetration testing has been done; this is a hobby project reviewed on a best-effort basis
- Discord slash-command parameters that accept free text (e.g. custom template names) are validated against known values where it matters, but this project doesn't claim to have caught every edge case
