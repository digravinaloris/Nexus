# Nexus

A full-featured Discord moderation & utility bot with a web dashboard — auto-moderation, anti-raid and anti-nuke protection, a full case/sanction system with appeals, ticket support, server backups, and a companion Android app for managing your server on the go.

Built with Python (discord.py) + Flask, backed by MongoDB Atlas, hosted on Render.

**[➕ Invite Nexus to your server](https://discord.com/oauth2/authorize?client_id=1510263826455466004)**

---

## Features

- **Moderation** — ban, kick, mute, warn, tempban, softban, bulk-delete messages, full sanction history
- **Case system** — every sanction gets a permanent case number (`/case view`, `/case edit`), exportable as CSV
- **Sanction appeals** — sanctioned members get a DM with a button to appeal directly in Discord; staff review and accept/deny from Discord or the web dashboard, with automatic reversal (unban, remove timeout, remove a warning) where possible
- **Auto-moderation** — anti-spam, anti-invite-link, anti-caps, a custom banned-words list (with a one-click import of a ready-made list), and a blocked-domains list
- **Anti-raid** — locks the server automatically if too many members join in a short window
- **Anti-nuke** — watches Discord's own audit log for a burst of destructive actions (bans, kicks, channel/role deletions) by the same person and quarantines them automatically
- **Account age gate** — optionally auto-kick accounts younger than a configurable number of days
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
| `/slowmode <channel> <seconds>` | Set or disable slowmode on a channel |
| `/roleadd` `/roleremove` | Add / remove a role from a member |
| `/reactionrole` | Create a reaction-role message |
| `/nickname <member>` | Change a member's nickname |
| `/groupnickname <role> <prefix>` | Add or remove a prefix on the nickname of every member with a role |

### Tickets
| Command | Access | Description |
|---|---|---|
| `/config ticket setup <panel_channel> <category> <support_role> [archive_category]` | Server owner | Post the "Open Ticket" button and configure where tickets go |
| `/config ticket disable` | Server owner | Turn off the ticket system |

Clicking the panel button opens a short form, then creates a private channel with a **Close Ticket** button (single tap, no delay). If an archive category was set, closed tickets are moved there (inheriting its permissions) instead of being deleted. The person who opened the ticket gets a 👍/👎 satisfaction survey after it closes.

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

### Music
| Command | Description |
|---|---|
| `/play` `/search` `/pause` `/resume` `/skip` `/stop` `/queue` `/volume` | Standard music controls, with YouTube search & autocomplete |

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

Every server only shows up for users who are genuinely an Administrator there, re-verified against Discord on every action — never trusted from the browser alone.

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
- **yt-dlp** + **imageio-ffmpeg** — music playback
- **pyotp** — the kill-switch's authenticator-app verification
- **Resend** — transactional email for the kill-switch (Render blocks outbound SMTP)
- **PyYAML** — dashboard translations (`en_us.yml`, `fr_fr.yml`)
- **Render** — hosting

---

## Companion Android App

Built with Kotlin + Jetpack Compose, connecting to the bot's REST API to let server admins moderate, check stats, and manage configuration from their phone. Each server gets its own API key via `/config apikey`.

---

## Known Issues

- **`/play` crashes (FFmpeg segfault)** — music playback can currently crash with a segfault on Render's infrastructure under some conditions. Under active investigation; if `/play` stops responding, `/skip` or `/stop` and try again.

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
