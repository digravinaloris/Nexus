"""owner_notify -- messages privés envoyés au propriétaire du bot (kill-switch, tentatives suspectes).

Extrait de main.py (étape 2b-2a). Le bot vient de `core.bot` (jamais `import main`).
"""
import discord

import core


async def get_owner_user():
    app_info = await core.bot.application_info()
    if app_info.owner:
        return app_info.owner
    if app_info.team:
        try:
            return await core.bot.fetch_user(app_info.team.owner_id)
        except discord.HTTPException:
            return None
    return None


async def notify_owner_killswitch(locked: bool):
    owner = await get_owner_user()
    if owner is None:
        return
    msg = (
        "🔒 Nexus has just been **locked down** across all servers via the email kill-switch."
        if locked else
        "🔓 Nexus has just been **unlocked** and is back to normal across all servers."
    )
    try:
        await owner.send(msg)
    except discord.HTTPException:
        pass


async def notify_owner_bruteforce(ip):
    owner = await get_owner_user()
    if owner is None:
        return
    msg = (
        f"🚨 **Possible intrusion attempt**: 5 incorrect codes were entered on a kill-switch "
        f"confirmation link (from IP `{ip}`). That link is now dead.\n\n"
        f"If this wasn't you, consider rotating your TOTP secret (`generate_totp_secret.py`) "
        f"and updating `TOTP_SECRET` on Render."
    )
    try:
        await owner.send(msg)
    except discord.HTTPException:
        pass
