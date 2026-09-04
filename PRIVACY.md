# Privacy Policy — Nexus

**Last updated:** July 8, 2026

This Privacy Policy explains what data the Nexus Discord bot ("the Bot", "we", "us") collects, why, and how it is handled. By using the Bot in any Discord server, you agree to this Policy.

## 1. Data We Collect

We only collect data needed for the Bot's features to function:

| Data | Purpose |
|---|---|
| Discord User ID, username | Identify users for moderation, warnings, applications |
| Warning counts & history | `/warn`, `/warnings`, `/warnlist` |
| Sanction records (ban, kick, mute, tempban) — type, reason, moderator, timestamp | Moderation history (`/history`), audit logs |
| Server (guild) ID & configuration | Autorole, allowed roles, safe roles, log channel, safemode password |
| Locked channels | `/lock`, `/vlock` and related unlock tracking |
| Reaction role mappings (message ID, emoji, role ID) | `/reactionrole` |
| Role application answers submitted via `/apply` (why you want the role, age, what you bring) | Forwarded to the server owner to review and accept/refuse |
| API key usage (companion Android app) | Authenticating server staff for remote moderation actions |

We do **not** collect or store:
- Message content outside of what's explicitly needed for a command or log (e.g., we don't log or read your regular chat messages)
- Passwords to your Discord account, email addresses, or payment information
- Voice audio

## 2. How Data Is Stored

Data is stored in a MongoDB Atlas database, accessible only to the Bot's backend, hosted on Render. The database is not publicly accessible and requires authenticated credentials to access.

## 3. How Data Is Used

Data is used strictly to operate the Bot's features: enforcing moderation actions, restoring settings after restarts, displaying history/warnings to authorized staff, managing reaction roles, and processing role applications. We do not sell, rent, or share your data with third parties, and we do not use it for advertising.

## 4. Application Data (`/apply`)

When you submit an application, your answers and Discord ID/username are sent as a direct message to the relevant server's owner so they can review and accept or refuse it. This data is not stored permanently in our database beyond what's needed to link the decision back to you (applicant ID, target role, server ID) while the application is pending.

## 5. Companion Android App

Server staff using the Android app authenticate via a private API key. The app only exposes data already visible to staff on Discord (member lists, warnings, server config) and does not collect additional personal data beyond what's listed above.

## 6. Data Retention

- Moderation and sanction history is kept indefinitely to preserve accurate records, unless a server owner or user requests deletion (see Section 8).
- Server configuration and reaction role data are kept as long as the Bot remains in the server, and removed if the Bot is kicked/removed.
- Locked channel records are removed once a channel is unlocked.

## 7. Third-Party Services

The Bot relies on the following third-party services, each with their own privacy practices:
- **Discord** (discord.com/privacy) — underlying platform
- **MongoDB Atlas** — database hosting
- **Render** — bot hosting
- **YouTube** (via yt-dlp) — music playback source

We are not responsible for the privacy practices of these third parties.

## 8. Your Rights

You can request access to, or deletion of, your data (warnings, sanction history, application data) associated with your Discord ID by contacting us (see Section 10). Note that deleting moderation records may affect a server's ability to enforce past decisions, and server owners may have independent reasons to retain records.

## 9. Children's Privacy

The Bot is not intended for use by anyone who does not meet Discord's own minimum age requirement (13+, or higher where required by local law). We do not knowingly collect data from users who don't meet this requirement beyond what Discord itself already requires.

## 10. Changes to This Policy

We may update this Privacy Policy from time to time. Continued use of the Bot after changes are published constitutes acceptance of the updated Policy.

## 11. Contact

For privacy questions or data requests, contact the bot owner via the GitHub repository: [github.com/digravinaloris/dc-bot](https://github.com/digravinaloris/dc-bot)
