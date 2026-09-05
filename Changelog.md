# Changelog

All notable changes to Nexus are documented here.

## [Unreleased]

### Added
- `/apply` — role application system: members apply via a form, the server owner reviews and accepts/refuses via buttons
- `/config` command group — server configuration (logs channel, autorole, API key, per-command role permissions)
- `/config allow` / `/config disallow` — let the server owner grant specific roles access to specific commands
- `/botlock` / `/botunlock` — server owner can lock/unlock the bot on their server
- Anti-raid protection — automatically locks a server if too many members join in a short window, with an alert to the server owner
- Per-server API keys (`/config apikey`) for the companion Android app, in addition to the master key
- `on_guild_join` — welcome DM to the server owner with setup instructions
- `on_guild_remove` — automatic cleanup of all stored data (config, warnings, sanctions, notes, reaction roles) when the bot is removed from a server
- `/help`, `/ping`, `/botinfo` — utility commands
- `/poll` — quick polls with up to 4 options or a simple 👍/👎
- `/slowmode` — set or disable channel slowmode
- `/nickname` — change a member's nickname
- `/groupnickname` — add or remove a prefix on the nickname of every member with a role
- `/note` / `/notes` — internal staff notes on a member, not visible to them
- `/softban` — kick a member and delete their recent messages
- `/purgeuser` — erase all stored data for a member on a server (Administrator only)
- `README.md` with full command reference, invite link, getting started guide, and legal links
- `TERMS.md` and `PRIVACY.md` published
- `.gitignore` to prevent secrets and local files from being committed
- **Web dashboard** (Discord OAuth2 login) — manage a server directly from the browser, restricted to servers where you're an Administrator and where Nexus is present
- Dashboard: logs channel, auto-role, and anti-raid exemption management
- Dashboard: per-command role permissions, with a mass-select UI and quick permission templates (built-in: Trial Moderator, Moderator, Helper, DJ)
- `/config template set` / `/config template list` — create and list custom, per-server command-permission templates, usable from both Discord and the dashboard
- `/config language` — set the default dashboard language for a server (English/French)
- Dashboard language switcher (EN/FR), backed by external `en_us.yml` / `fr_fr.yml` locale files
- Dashboard: mobile API key viewer (masked, reveal-on-click) and one-click regeneration
- Dashboard: bot lockdown toggle (server owner only)
- **Audit log** — every dashboard change (config, permissions, templates, lockdown, API key) is recorded with who/what/when and shown in a dedicated dashboard panel (server owner only)
- Optional Discord logging of dashboard actions — when enabled, dashboard changes are also posted to the server's logs channel
- **Ticket system** — `/config ticket setup` posts a persistent "Open Ticket" button; clicking it creates a private channel visible to the user and a configured support role, with a "Close Ticket" button. `/config ticket disable` to turn it off
- **Live member count** — `/config membercount` renames a chosen voice channel to show the server's current member count, refreshed periodically

### Changed
- Warnings are now scoped per server (previously shared across all servers a user was in)
- Bot lock (`bot.locked`) is now per-server instead of a single global flag
- The bot's lock/logs/API behavior no longer relies on a single hardcoded owner account — fully multi-server
- `/apply` DMs now go to the server owner instead of the bot developer
- Auto-moderation exemptions are now role/permission-based instead of exempting one hardcoded account
- Mobile app action logs now post to each server's configured logs channel instead of a single hardcoded channel
- Project renamed from **Jello Bello** to **Nexus**
- `/config view` now splits long permission lists across multiple embed fields instead of one, avoiding Discord's 1024-character field limit

### Removed
- `/safemode` and the old password-protected `/config` system
- Hardcoded default configuration for a single server

### Fixed
- `on_member_join` no longer grants the configured auto-role while the server is in an active anti-raid lockdown
- Dashboard now warns when an auto-role or applied permission role has sensitive permissions (Administrator, Manage Server, Ban/Kick, Manage Roles)
- Dashboard permission removal no longer silently fails for commands added with different casing or outside the curated command list
- Bot no longer crash-loops when Discord/Cloudflare rate-limits the login (429) — it now waits with an increasing backoff instead of exiting the process
- Dashboard no longer 500s when Discord's `/users/@me/guilds` endpoint returns a non-list response (e.g. during a rate limit)

### Security
- Purged two exposed Discord bot tokens from the entire git history
- Regenerated the bot token
- Per-command permissions can now be restricted to specific roles, separate from Discord's default permission set
- `/config allow` / `/config disallow` and `/botlock` / `/botunlock` restricted to the server owner specifically, not just Administrator
- Dashboard OAuth2 flow validates a signed `state` parameter to prevent CSRF
- Dashboard session cookies are `Secure`, `HttpOnly`, and `SameSite=Lax`
- Dashboard re-validates admin/owner status against Discord's API (not client-supplied data) on every sensitive action
- Dashboard actions that change roles, channels, or commands are re-validated against the real objects of the target server before being written, preventing cross-server or forged-ID writes
- Ticket and template systems restrict structural changes (`/config ticket setup`, `/config template set`) to the server owner
- See `SECURITY.md` for the full security policy and how to report a vulnerability

