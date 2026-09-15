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
- **Ticket system** — `/config ticket setup` posts a persistent "Open Ticket" button; clicking it opens a short form (what do you need help with), then creates a private channel visible to the user and a configured support role, with the form's answer posted automatically and a "Close Ticket" button
- Ticket archiving — `/config ticket setup` accepts an optional archive category; closed tickets are moved there (read-only for the opener) instead of being deleted, so a history is kept
- **Live member count** — `/config membercount` renames a chosen voice channel to show the server's current member count, refreshed periodically
- **Global emergency kill-switch** — a daily email with a single-use, 30-minute link; opening it requires a 6-digit authenticator (TOTP) code before anything happens, and a correct code locks (or unlocks) the bot across every server at once via one central check, independent of any single server or Discord account
- `/admin killswitch status` / `/admin killswitch resend` — check the global lock state or request a fresh kill-switch email on demand, restricted to the real Discord application owner (verified via Discord itself, not a stored ID)
- Kill-switch brute-force protection — a link is destroyed after 5 wrong codes, and the owner gets an immediate DM if that happens
- IP address logging on every kill-switch page view and attempt, recorded in the audit log
- **Case ID system** — every sanction (starting with bans) gets a per-server sequential case number; `/case view` looks one up, and `/history` now shows case numbers too
- **Ban appeals** — `/config appeals` sets a review channel; ban DMs then include a one-time appeal link (no login required), and `/appeal accept` / `/appeal deny` let staff resolve it, unbanning and notifying the user automatically on accept
- Ticket close button now works in a single tap: no more 5-second delay, and the button disables itself immediately so it can't be clicked twice
- Ticket archiving now uses Discord's own permission sync (`sync_permissions`) instead of a manual per-user override, so an archive category's own permissions fully decide who can still see closed tickets
- Boost thank-you messages — `/config boostmessage` sets where a thank-you embed is posted when someone boosts the server (defaults to the logs channel)
- **Sticky messages** — `/config sticky set` keeps a message pinned to the bottom of a channel, reposting it (with a cooldown) whenever new messages push it down; `/config sticky remove` to stop
- Public `/status` page — no login required, shows whether the bot is online, server count, latency, and uptime
- **Detailed activity logs** (opt-in via `/config detailedlogs`) — voice channel joins/leaves/moves, nickname changes, and role changes now post to the logs channel
- **Custom banned words list** — `/config badwords add/remove/list` for a per-server list, plus `/config badwords import` to pull a ready-made English or French list from a public GitHub template (LDNOOBW, CC-BY-4.0) as a starting point
- **Ticket satisfaction survey** — after a ticket is closed, the person who opened it gets a DM with a quick 👍/👎 on their experience
- **Anti-nuke** (opt-in via `/config antinuke`) — watches Discord's own audit log for a burst of destructive actions (bans, kicks, channel/role deletions) by the same person within 60 seconds; strips their roles and locks the bot on that server, then DMs the owner with what happened
- **Server backups** — `/backup create` snapshots roles, channels, and role-based permissions; `/backup list` shows saved backups; `/backup restore` re-creates whatever's missing (matched by name), never deleting or overwriting anything
- **Weekly digest** (opt-in via `/config digest`) — a DM to the server owner roughly every 7 days with member count and a breakdown of moderation actions taken that week
- **Sanction appeals for every sanction type** (not just bans) — `ban`, `kick`, `mute`, `warn`, `tempban`, and `softban` now DM the sanctioned person a case number and, if appeals are enabled, an in-Discord **Appeal this** button that opens a form and submits directly (no website needed); `/appeal accept` now reverses the specific sanction type automatically where possible (unban, remove timeout, remove a warning)
- `/case edit` — correct the reason on an existing case after the fact
- `/clear` now requires the number of messages explicitly, no more accidental default
- **Invite tracking** — `/invites leaderboard` and `/invites who <member>` show who invited whom
- `/export sanctions` / `/export audit` — download a server's moderation history or dashboard audit log as a CSV file
- A small 2-second cooldown between commands per person, to prevent accidental spam
- **Account age gate** — `/config accountage` auto-kicks (with a DM explaining why) accounts younger than a configurable number of days
- **Blocked domains list** — `/config domains add/remove/list`, checked against links in messages the same way the banned words list is
- Dashboard: a case browser page, filterable by sanction type
- Dashboard: an appeals review page — accept or deny sanction appeals from the browser instead of only Discord commands
- Dashboard: an onboarding checklist showing what isn't configured yet
- Dashboard: the audit log panel is now a real multi-admin activity feed — it merges dashboard changes with sanctions issued through commands (bans, kicks, etc.), not just web actions
- Dashboard: an "open tickets" panel listing currently open ticket channels with direct links
- `/setup` — a guided setup wizard with dropdown menus, so you don't have to know every `/config` command individually
- **Temporary voice channels** — `/config jointocreate` makes joining a chosen channel spawn a private one, deleted automatically once empty
- **Dropdown role menus** — `/config rolemenu additem` / `post` build a modern select-menu role picker (alternative to reaction roles)
- `/schedule` — post a message to a channel at a later time
- **Warn escalation** — `/config warnescalation` auto-mutes then auto-kicks at configurable warning counts
- `/case note` — attach follow-up notes to a case without overwriting the original reason
- `/backup preview` — see a backup's roles and channels before restoring it
- `/config automod` — tune spam, caps, and raid detection thresholds per server (previously hardcoded)
- Dashboard: a moderation activity chart (sanctions per day over the last 30 days), drawn as inline SVG with no external JS
- A test suite (`pytest tests/`) covering the pure utility functions, plus a GitHub Actions workflow that runs it on every push and PR

### Changed
- Warnings are now scoped per server (previously shared across all servers a user was in)
- Pure helper functions moved into `utils.py` so they can be tested without starting the bot or connecting to MongoDB
- Moderation logs are now sent through a Discord webhook (created and cached automatically), falling back to a normal message if the bot lacks Manage Webhooks
- `/history`, `/banlist`, `/mutelist`, and `/warnlist` are now paginated with buttons instead of risking hitting Discord's embed limits — `/banlist` previously truncated silently at 25 entries
- Music extraction now tries without cookies first, falling back to cookies only if needed — works around the currently broken `tv_downgraded` YouTube client (yt-dlp issue #17389)
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
- Bot now shuts down gracefully on SIGTERM (sent by Render on redeploy) instead of being killed mid-operation, and closes its MongoDB connection cleanly
- Commands now have a short cooldown, preventing accidental double-clicks or spam
- The appeal button in sanction DMs is rate-limited against repeated clicks
- `on_member_join` no longer grants the configured auto-role while the server is in an active anti-raid lockdown
- Dashboard now warns when an auto-role or applied permission role has sensitive permissions (Administrator, Manage Server, Ban/Kick, Manage Roles)
- Dashboard permission removal no longer silently fails for commands added with different casing or outside the curated command list
- Bot no longer crash-loops when Discord/Cloudflare rate-limits the login (429) — it now waits with an increasing backoff instead of exiting the process
- Dashboard no longer 500s when Discord's `/users/@me/guilds` endpoint returns a non-list response (e.g. during a rate limit)
- Kill-switch emails now send over HTTPS (Resend API) instead of raw SMTP, which Render blocks outbound on free web services
- Kill-switch confirmation page no longer 500s from a naive/aware datetime comparison when reading the token's expiry back from MongoDB
- Kill-switch email/code failures are now reported back to Discord instead of silently failing while claiming success

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
- Global kill-switch requires two independent factors (a single-use emailed link + a TOTP code) and is enforced through one central command check rather than scattered per-command guards
- See `SECURITY.md` for the full security policy and how to report a vulnerability

