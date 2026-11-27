# Nexus

A full-featured Discord moderation & utility bot with a web dashboard — auto-moderation, anti-raid and anti-nuke protection, a full case/sanction system with appeals, ticket support, server backups, and a companion Android app for managing your server on the go.

Built with Python (discord.py) + Flask, backed by MongoDB Atlas, hosted on Render.

**[➕ Invite Nexus to your server](https://discord.com/oauth2/authorize?client_id=1510263826455466004)**

**[Dashboard](https://nexus-1qqu.onrender.com/dashboard)**

---

## Features

- **Moderation** — ban, kick, mute, warn, tempban, softban, bulk-delete messages, full sanction history
- **Case system** — every sanction gets a permanent case number (`/case view`, `/case edit`), exportable as CSV
- **Sanction appeals** — sanctioned members get a DM with a button to appeal directly in Discord; staff review and accept/deny from Discord or the web dashboard, with automatic reversal (unban, remove timeout, remove a warning) where possible
- **Auto-moderation** — anti-spam, anti-invite-link, anti-caps, a custom banned-words list (with a one-click import of a ready-made list), and a blocked-domains list
- **Anti-raid** — locks the server automatically if too many members join in a short window
- **Anti-nuke** — watches Discord's own audit log for a burst of destructive actions (bans, kicks, channel/role deletions) by the same person and quarantines them automatically
- **Account age gate** — optionally auto-kick accounts younger than a configurable number of days
- **Cloudflare Turnstile verification** — personal, expiring verification links; Verified/Unverified roles; optional post-verification role; account-age checks and configurable cleanup of members who never verify
- **Server calendar** — create events with role notifications, timezone-aware dates, and automatic 24-hour / 1-hour reminders; manage events in Discord or the dashboard
- **Ticket system** — a button opens a private channel (with an optional intake form), a satisfaction survey DM after closing, and optional archiving instead of deletion
- **Server backups** — snapshot roles, channels, and permissions; restore whatever's missing without ever deleting anything
- **Global emergency kill-switch** — a daily email with a one-time link, gated behind a 6-digit authenticator code, to lock the bot across every server at once if something goes wrong
- **Web dashboard** — manage a server's config, permissions, cases, and open tickets from a browser, in English or French
- **Invite tracking**, **sticky messages**, **weekly DM digest**, **boost thank-yous**, **live member count**, **detailed activity logs** (voice, nicknames, roles)
- **Reaction roles** and a **role-application system** (`/apply`)
- **Per-command permissions** — the server owner decides exactly which role can use which command
- **Companion Android app** — moderate your server, view stats, and manage config from your phone

Nexus works across multiple servers — each has its own independent configuration, cases, permissions, and dashboard access.

---

## Documentation

- [Changelog](docs/releases/CHANGELOG.md)
- [Privacy policy](PRIVACY.md)
- [Terms of service](TERMS.md)
- [Security policy](SECURITY.md)

## Getting Started

1. [Invite the bot](https://discord.com/api/oauth2/authorize?client_id=1510263826455466004&permissions=1099917519958&scope=bot%20applications.commands) to your server.
2. As Administrator, run:
   - `/config logs <channel>` — where moderation logs get posted
   - `/config autorole <role>` — role given automatically to new members (optional)
3. As server **owner**, decide who's allowed to do what:
   - `/config allow <command> <role>` — let a specific role use a specific command (e.g. `/config allow ban @Moderator`)
   - Skip this if the default Discord permissions (Ban Members, Kick Members, etc.) already work for you
4. Log in to the [web dashboard](#web-dashboard) to see a checklist of what's not configured yet.
5. Turn on any extra protection you want with `/feature enable <name>` — see [Feature Toggles](#feature-toggles).
6. Run `/help` any time to see the full command list in Discord.

---

## Commands

### Moderation
| Command | Description |
|---|---|
| `/ban` `/unban` | Ban / unban a member |
| `/kick` | Kick a member |
| `/softban` | Kick a member and delete their recent messages |
| `/mute` `/unmute` | Timeout a member |
| `/tempban <duration>` | Temporary ban (`30m`, `2h`, `1d`, `1w`), lifted automatically |
| `/warn` `/unwarn` `/warnings` `/warnlist` | Manage warnings |
| `/note` `/notes` | Internal staff notes on a member (not visible to them) |
| `/banlist` `/mutelist` | List active bans / mutes |
| `/history` | Full sanction history for a member |
| `/clear <amount>` | Bulk-delete messages, with user/role/bot filters |
| `/purgeuser <user_id>` | Erase all stored data for a member on this server — Administrator only |

Every sanction (ban, kick, mute, warn, tempban, softban) DMs the person a case number and, if appeals are enabled, an **Appeal this** button.

### Cases & Appeals
| Command | Access | Description |
|---|---|---|
| `/case view <case_id>` | — | Look up a case by its number |
| `/case edit <case_id>` | Administrator | Correct a case's reason |
| `/appeal accept <case_id>` | Administrator | Accept an appeal — reverses the sanction automatically where possible |
| `/appeal deny <case_id>` | Administrator | Deny an appeal |
| `/export sanctions` | Administrator | Download the full moderation history as CSV |
| `/export audit` | Administrator | Download the dashboard/activity audit log as CSV |

### Channels & Roles
| Command | Description |
|---|---|
| `/lock` `/unlock` | Lock / unlock a text channel |
| `/vlock` `/vunlock` | Lock / unlock a voice channel |
| `/lockedchannels` | List currently locked channels |
| `/slowmode <channel> <seconds>` | Set or disable slowmode on a text channel |
| `/roleadd` `/roleremove` | Add / remove a role from a member |
| `/reactionrole` | Create a reaction-role message |
| `/nickname <member>` | Change a member's nickname |
| `/groupnickname <role> <prefix>` | Add or remove a prefix on the nickname of every member with a role |

### Tickets
| Command | Access | Description |
|---|---|---|
| `/ticket setup <panel_channel> <category> <support_role> [archive_category]` | Server owner | Post the "Open Ticket" button and configure where tickets go |
| `/ticket disable` | Server owner | Turn off the legacy/default ticket panel |
| `/ticket panel-create` | Server owner | Create another panel with its own channel, category, support role, archive category and opening message |
| `/ticket panel-list` | Server owner | List configured panels and their IDs |
| `/ticket panel-remove <panel_id>` | Server owner | Remove a panel without affecting existing tickets |
| `/ticket panel-questions <panel_id> <question1> [question2-5]` | Server owner | Configure up to five required questions for that panel |
| `/ticket reopen` | Ticket support role or server staff | Reopen an archived ticket and restore its original category and access |
| `/ticket priority <priority>` | Ticket support role or staff | Set the current ticket to Low, Normal, High or Urgent |
| `/ticket assign <member>` | Ticket support role or staff | Assign the current ticket to a staff member |
| `/ticket unassign` | Ticket support role or staff | Remove the current ticket assignment |

Ticket priorities and staff assignments are stored in the channel topic and remain attached to the ticket. Use `/ticket assign @member` to assign a ticket to a support-role member or staff member with Manage Channels; the assignee receives explicit access to the ticket. Use `/ticket unassign` to remove the assignment and its member-specific channel access. Support staff or members with Manage Channels can change them from inside the ticket using `/ticket priority`. Each additional panel routes tickets to its own category and support role. Panel IDs are stored in ticket channel topics so closing and archiving use the correct panel configuration. The legacy ticket setup remains supported. Clicking a panel opens its custom intake form (up to five required questions configured with `/ticket panel-questions`) and creates a private channel with a **Close Ticket** button. Answers are included in the ticket embed. Closed tickets are archived when an archive category is configured, otherwise deleted. Archived tickets can be reopened with `/ticket reopen` by support staff or server staff; reopening restores the original panel category and ticket access. The existing satisfaction survey and HTML transcript delivery are preserved. Transcripts use a Discord-inspired layout with avatars, timestamps, bot labels, embeds and attachment previews.

### Member Verification

Nexus can gate new members behind Cloudflare Turnstile. Configure the **Verified** and **Unverified** roles in the dashboard's **Settings → Verification** section, then configure the verification channel's Discord permissions yourself: keep Unverified members out of normal channels and allow them into the verification channel. Nexus manages roles, not channel visibility.

In the verification channel, run `/config verification post`, then enable the feature with `/feature enable verification`. Members receive a personal verification link; links expire, are single-use, and have a limited number of attempts. After a successful Cloudflare check, Nexus grants Verified, removes Unverified, and can grant an additional configured role. Optional account-age limits and automatic kicks for members who remain unverified are configurable. Turnstile requires `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY`, and a public base URL (or a valid `DISCORD_REDIRECT_URI` from which it can be derived). If Turnstile is unavailable or not configured, verification fails closed.

### Server Calendar

The `/calendar` command group manages upcoming server events. Calendar management is available to administrators, configured calendar-manager roles, and members granted the corresponding command permission.

| Command | Description |
|---|---|
| `/calendar add <title> <role> <date> [time] [description]` | Create an event and announce it with a role mention |
| `/calendar list` | List the next 15 upcoming events |
| `/calendar edit <event_id>` | Edit an event's title, role, date, time, or description |
| `/calendar delete <event_id>` | Delete an event |
| `/calendar timezone <timezone>` | Set the server's IANA timezone (default: `Europe/Paris`) |
| `/calendar channel [channel]` | Set or clear the announcement channel |
| `/calendar manager <role> <add/remove>` | Manage calendar-manager roles (administrator only) |

Events are stored per server. Nexus sends automatic reminders roughly 24 hours and 1 hour before an event. The dashboard also includes a calendar page for managing events.

### GitHub Release Announcements

The bot owner can configure one announcement channel on their own server with `/updates channel <channel>`. Nexus posts plain-text announcements for new stable releases; a GitHub webhook can deliver them immediately, with the 10-minute polling check retained as a fallback. Use `/updates check` to publish the latest release immediately, `/updates status` to view the configuration, and `/updates disable` to turn it off. Only the Discord application owner who also owns the configured server can manage this feature.

**Instant webhook setup (optional):** Set `GITHUB_RELEASE_WEBHOOK_SECRET` in the bot host's environment to a long, random secret. In the GitHub repository, open **Settings → Webhooks → Add webhook**, set the Payload URL to `https://YOUR_NEXUS_HOST/webhooks/github/releases`, Content type to `application/json`, and enter the same secret. Choose **Let me select individual events → Releases** and save the webhook. Configure the announcement channel first with `/updates channel`. Nexus verifies the signature and ignores drafts, prereleases, unrelated repositories, and duplicate delivery IDs.

If delivery fails, see [GitHub release webhook troubleshooting](docs/github-release-webhook-troubleshooting.md).

### Invites, Backups & Data
| Command | Access | Description |
|---|---|---|
| `/invites leaderboard` | — | Top inviters on this server |
| `/invites who <member>` | — | Who invited a specific member |
| `/backup create` | Server owner | Snapshot roles, channels, and permissions |
| `/backup list` | Server owner | List saved backups |
| `/backup restore <backup_id>` | Server owner | Re-create whatever's missing from a backup (never deletes) |

### Server Configuration (`/config`)
| Command | Access | Description |
|---|---|---|
| `/config logs <channel>` | Administrator | Set the logs channel |
| `/config autorole <role>` | Administrator | Role given automatically to new members |
| `/config accountage <days>` | Administrator | Auto-kick accounts younger than X days (0 to disable) |
| `/config appeals <channel>` | Administrator | Where sanction appeals are reviewed |
| `/config boostmessage <channel>` | Administrator | Where boost thank-you messages are posted |
| `/config membercount <channel>` | Administrator | Voice channel that shows the live member count |
| `/config antinuke <threshold>` | Administrator | Set the anti-nuke sensitivity (see [Feature Toggles](#feature-toggles) to turn it on) |
| `/config badwords add/remove/list` | Administrator | Manage the custom banned-words list |
| `/config badwords import <language>` | Administrator | Import a ready-made banned-words list |
| `/config domains add/remove/list` | Administrator | Manage the blocked-domains list |
| `/config sticky set/remove <channel>` | Administrator | A message that stays pinned to the bottom of a channel |
| `/config template set/list` | Server owner | Custom, reusable command-permission templates |
| `/config language <en/fr>` | Server owner | Default dashboard language for this server |
| `/config apikey` | Administrator | Generate an API key for the mobile app |
| `/config view` | Administrator | View the current configuration |
| `/config allow <command> <role>` | **Server owner only** | Let a specific role use a specific command |
| `/config disallow <command> <role>` | **Server owner only** | Remove that permission |
| `/botlock` `/botunlock` | **Server owner only** | Lock / unlock the bot on this server |

### Feature Toggles
| Command | Description |
|---|---|
| `/feature enable <name>` | Turn on: `detailed_logs`, `antinuke`, `weekly_digest`, or `dashboard_logging` |
| `/feature disable <name>` | Turn any of those back off |
| `/feature list` | See what's currently on or off |

### Utilities
| Command | Description |
|---|---|
| `/apply <role>` | Apply for a role via a short form; the server owner accepts or refuses |
| `/userinfo` `/serverinfo` | Member / server information |
| `/broadcast` | Send an announcement as the bot |
| `/poll <question>` | Create a quick poll |
| `/help` | List all available commands |
| `/ping` | Check the bot's latency |
| `/botinfo` | Bot stats: servers, uptime, latency |


### Bot Owner Only
| Command | Description |
|---|---|
| `/admin killswitch status` | Check whether the bot is globally locked down |
| `/admin killswitch resend` | Send a fresh emergency kill-switch email right now |

---

## Web Dashboard

Log in with Discord at the bot's URL (`/dashboard`) to manage any server where you're an Administrator. The dashboard mirrors and extends what's available in Discord:

- Logs channel, auto-role, and anti-raid exemptions
- Per-command permissions with a mass-select UI and quick templates
- A case browser and an appeals review page (accept/deny without touching Discord)
- A live view of currently open tickets
- A setup checklist showing what isn't configured yet
- A multi-admin activity feed (dashboard changes + sanctions, merged)
- Server lockdown, mobile API key management
- English and French, switchable per-session or set as a server default

Every dashboard request fetches the user's current server permissions directly from Discord. If Discord cannot confirm them, dashboard access is denied rather than relying on stale cached permissions. OAuth token and identity responses are validated; malformed responses are rejected without creating an authenticated session.

---

## How permissions work

1. **Administrators** can always use every command.
2. If the server owner has configured specific roles for a command (`/config allow`), only those roles (or an admin) can use it.
3. Otherwise, the command falls back to its default Discord permission.
4. While the bot is locked on this server (`/botlock`, automatically after a detected raid or anti-nuke trigger, or globally via the [emergency kill-switch](SECURITY.md)) — only Administrators (or, for the global kill-switch, only the bot's real owner) can use protected commands until it's unlocked again.

---

## Tech Stack

- **Python** + [discord.py](https://discordpy.readthedocs.io/)
- **Flask** — the web dashboard and the REST API powering the Android companion app
- **MongoDB Atlas** — cases, config, tickets, backups, appeals, and everything else
- **pyotp** — the kill-switch's authenticator-app verification
- **Resend** — transactional email for the kill-switch (Render blocks outbound SMTP)
- **PyYAML** — dashboard translations (`en_us.yml`, `fr_fr.yml`)
- **Render** — hosting

---

## Companion Android App

Built with Kotlin + Jetpack Compose, connecting to the bot's REST API to let server admins moderate, check stats, and manage configuration from their phone. Each server gets its own API key via `/config apikey`. API keys are checked using constant-time comparisons.

The mobile REST API validates Discord guild and user IDs before processing requests, rejects malformed moderation and broadcast payloads, bounds moderation reasons and message lengths, validates FAQ and feature-toggle payloads, limits mute durations to Discord's 28-day maximum, and clamps audit/case pagination. Appeal reviews are claimed atomically so concurrent dashboard and mobile actions cannot resolve the same appeal twice. Keep each server's API key private and regenerate it from the dashboard or `/config apikey` if it is exposed.

---

## Known Issues


---

## Support

Found a bug or have a feature request? Open an [issue on GitHub](https://github.com/digravinaloris/Nexus/issues).

Found a **security** issue? Please don't open a public issue — see [SECURITY.md](SECURITY.md) for how to report it privately.

---

## License

All rights reserved. This project's source code is not currently licensed for reuse or redistribution.

---

## Legal

- [Terms of Service](https://github.com/digravinaloris/Nexus/blob/main/TERMS.md)
- [Privacy Policy](https://github.com/digravinaloris/Nexus/blob/main/PRIVACY.md)
- [Security Policy](https://github.com/digravinaloris/Nexus/blob/main/SECURITY.md)
