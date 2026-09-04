# Terms of Service — Nexus

**Last updated:** July 8, 2026

These Terms of Service ("Terms") govern your access to and use of the Nexus Discord bot ("the Bot", "we", "us"). By adding the Bot to a Discord server, or by interacting with it in any server where it is present, you agree to these Terms. If you do not agree, do not use the Bot.

## 1. Eligibility

You must meet Discord's own minimum age requirement (13 years old, or higher where required by local law) to use Discord and, by extension, the Bot. Server owners are responsible for ensuring their server complies with Discord's Terms of Service and Community Guidelines.

## 2. Description of Service

Nexus is a general-purpose Discord bot providing:

- Moderation tools (ban, kick, mute, warnings, channel locks, auto-moderation, anti-raid protection)
- Utility commands (server/user info, broadcasts, sanction history)
- Reaction roles
- A role application system (`/apply`)
- Music playback
- An optional companion Android app connected through a REST API for server owners/staff

The Bot is provided "as is" and is a personal/independent project, not an official Discord product.

## 3. Data We Collect and Store

To operate, the Bot stores the following data in a MongoDB database:

- **Moderation data**: warnings, ban/kick/mute/tempban records, and the moderator or system responsible for each action
- **Server configuration**: log channel, autorole, allowed/safe roles, and related settings set by server admins
- **Locked channels**: which channels/voice channels are currently locked and by whom
- **Reaction roles**: message IDs, emojis, and associated roles
- **Role applications**: the answers submitted through `/apply`, temporarily processed to notify server owners, along with the applicant's Discord ID and username

We do **not** collect message content beyond what is explicitly required for the features above (e.g., moderation logs), and we do not sell or share this data with third parties.

## 4. Companion Android App & API

Server staff may optionally use the companion Android app to manage their server remotely. This app communicates with the Bot through a private REST API secured by an API key. Access to this API is limited to authorized staff of each server and is not exposed publicly.

## 5. Server Owner & Moderator Responsibility

The Bot is a tool. Actions taken through the Bot (bans, kicks, mutes, warnings, role changes, broadcasts, etc.) are performed at the direction of, and are the sole responsibility of, the server owners and staff who configure and use it. We are not responsible for moderation decisions made by individual server administrators using the Bot.

## 6. Automated Moderation

The Bot includes automated systems (anti-spam, anti-invite-link, anti-caps, anti-raid detection) that may take action without direct human review at the moment they trigger. Server owners are responsible for reviewing and adjusting these settings to fit their community.

## 7. Availability

The Bot is hosted on third-party infrastructure (Render) and depends on external services (Discord API, MongoDB Atlas, YouTube for music playback). We do not guarantee uninterrupted availability, and the Bot may be unavailable due to maintenance, hosting limits, outages, or changes to third-party services beyond our control.

## 8. Acceptable Use

You agree not to:

- Use the Bot to violate Discord's Terms of Service or Community Guidelines
- Attempt to exploit, abuse, reverse-engineer, or interfere with the Bot's operation or API
- Use the Bot to harass, spam, or harm other users or servers

We reserve the right to restrict or block access to the Bot for any server or user that violates these Terms.

## 9. Limitation of Liability

The Bot is provided free of charge, without warranty of any kind. To the fullest extent permitted by law, we are not liable for any damages, data loss, or disruption arising from your use of the Bot, including but not limited to moderation errors, downtime, or third-party service failures.

## 10. Changes to the Bot and These Terms

We may update, modify, or discontinue features of the Bot, and may update these Terms, at any time. Continued use of the Bot after changes are published constitutes acceptance of the updated Terms.

## 11. Termination

We may remove the Bot from any server, or restrict access for any user, at our discretion, without prior notice, particularly in cases of abuse or violation of these Terms.

## 12. Contact

For questions about these Terms, or to request removal of your data, please contact the bot owner via the support server or GitHub repository: [github.com/digravinaloris/dc-bot](https://github.com/digravinaloris/dc-bot)
