# Nexus

A full-featured Discord moderation & utility bot — auto-moderation, anti-raid protection, tempbans, reaction roles, a role-application system, music playback, and a companion Android app for managing your server on the go.

Built with Python (discord.py) + Flask, backed by MongoDB Atlas, hosted on Render.

**[➕ Invite Nexus to your server](https://discord.com/oauth2/authorize?client_id=1510263826455466004)**

---

## Features

- **Moderation** — ban, kick, mute, warn, tempban, clear messages, full sanction history
- **Auto-moderation** — anti-spam, anti-invite-link, anti-caps, all configurable
- **Anti-raid** — automatically locks the server if too many members join in a short window, with an instant alert to the server owner
- **Channel locking** — lock/unlock text or voice channels on demand
- **Reaction roles** — self-assignable roles via emoji reactions
- **Role applications** (`/apply`) — members apply through a form, the server owner reviews and accepts/refuses with one click
- **Per-command permissions** — the server owner decides exactly which role can use which command
- **Music** — play, queue, and search YouTube audio directly in voice channels
- **Companion Android app** — moderate your server, view stats, and manage config from your phone

Nexus works across multiple servers — each server has its own independent configuration, sanctions, warnings, and permissions.

---

## Getting Started

1. [Invite the bot](https://discord.com/api/oauth2/authorize?client_id=1510263826455466004&permissions=1099917519958&scope=bot%20applications.commands) to your server.
2. You (the server owner) get a DM with a quick setup summary. As Administrator, run:
   - `/config logs <channel>` — where moderation logs get posted
   - `/config autorole <role>` — role given automatically to new members (optional)
3. As server **owner**, decide who's allowed to do what:
   - `/config allow <command> <role>` — let a specific role use a specific command (e.g. `/config allow ban @Moderator`)
   - Skip this if the default Discord permissions (Ban Members, Kick Members, etc.) already work for you
4. (Optional) Run `/config apikey` to get an API key for the [companion Android app](#companion-android-app).
5. Run `/help` any time to see the full command list in Discord.

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
| `/note` `/notes` | Add / view internal staff notes on a member (not visible to them) |
| `/banlist` `/mutelist` | List active bans / mutes |
| `/history` | Full sanction history for a member |
| `/clear` | Bulk-delete messages, with user/role/bot filters |
| `/purgeuser <user_id>` | Erase all stored data (warnings, sanctions, notes) for a member on this server — Administrator only |

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

### Applications & Utilities
| Command | Description |
|---|---|
| `/apply <role>` | Apply for a role via a short form; the server owner accepts or refuses |
| `/userinfo` `/serverinfo` | Member / server information |
| `/broadcast` | Send an announcement as the bot |
| `/poll <question>` | Create a quick poll (up to 4 options, or a simple 👍/👎) |
| `/help` | List all available commands |
| `/ping` | Check the bot's latency |
| `/botinfo` | Bot stats: servers, uptime, latency |

### Music
| Command | Description |
|---|---|
| `/play` `/search` `/pause` `/resume` `/skip` `/stop` `/queue` `/volume` | Standard music controls, with YouTube search & autocomplete |

### Server Configuration (`/config`)
| Command | Access | Description |
|---|---|---|
| `/config logs <channel>` | Administrator | Set the logs channel |
| `/config autorole <role>` | Administrator | Role given automatically to new members |
| `/config apikey` | Administrator | Generate an API key for the mobile app |
| `/config view` | Administrator | View the current configuration |
| `/config allow <command> <role>` | **Server owner only** | Let a specific role use a specific command |
| `/config disallow <command> <role>` | **Server owner only** | Remove that permission |
| `/botlock` | **Server owner only** | Lock the bot on this server |
| `/botunlock` | **Server owner only** | Unlock the bot on this server |

By default, commands fall back to their standard Discord permission (e.g. Ban Members for `/ban`). The server owner can override this per-command with `/config allow`/`disallow` to grant access to specific roles instead.

---

## How permissions work

1. **Administrators** can always use every command.
2. If the server owner has configured specific roles for a command (`/config allow`), only those roles (or an admin) can use it.
3. Otherwise, the command falls back to its default Discord permission.
4. While the bot is locked (`/botlock`, or automatically after a detected raid), only Administrators can use protected commands until `/botunlock`.

---

## Tech Stack

- **Python** + [discord.py](https://discordpy.readthedocs.io/)
- **Flask** — REST API powering the Android companion app
- **MongoDB Atlas** — warnings, sanctions, configuration, locked channels, reaction roles
- **yt-dlp** + **imageio-ffmpeg** — music playback
- **Render** — hosting

---

## Companion Android App

Built with Kotlin + Jetpack Compose, connecting to the bot's REST API to let server admins moderate, check stats, and manage configuration from their phone. Each server gets its own API key via `/config apikey`.

---

## Known Issues

- **`/play` crashes (FFmpeg segfault)** — music playback can currently crash with a segfault on Render's infrastructure under some conditions. Under active investigation; if `/play` stops responding, `/skip` or `/stop` and try again.

---

## Support

Found a bug or have a feature request? Open an [issue on GitHub](https://github.com/digravinaloris/dc-bot/issues).

---

## License

All rights reserved. This project's source code is not currently licensed for reuse or redistribution.

---

## Legal

- [Terms of Service](https://github.com/digravinaloris/Nexus/blob/main/TERMS.md)
- [Privacy Policy](https://github.com/digravinaloris/Nexus/blob/main/PRIVACY.md)
