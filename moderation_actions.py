"""moderation_actions -- actions Discord liées aux appels de sanction.

Extrait de main.py (étape 2b-1). Le bot est lu via `core.bot` (jamais `import main`).
"""
import discord

import core
from sanctions_store import get_warns, set_warns


async def reverse_sanction(guild, sanction_type, user_id):
    """Tente d'annuler les effets d'une sanction acceptée en appel. Retourne
    une courte description de ce qui a été fait (ou pas) pour informer le
    staff, même quand aucune action automatique n'est possible."""
    if sanction_type in ("ban", "tempban", "softban"):
        try:
            await guild.unban(discord.Object(id=int(user_id)), reason="Appeal accepted")
            return "user unbanned"
        except discord.HTTPException:
            return "user was already not banned"
    if sanction_type == "mute":
        member = guild.get_member(int(user_id))
        if member is not None and member.timed_out_until:
            try:
                await member.timeout(None, reason="Appeal accepted")
                return "timeout removed"
            except discord.HTTPException:
                return "couldn't remove the timeout"
        return "user isn't currently timed out"
    if sanction_type == "warn":
        count = get_warns(guild.id, user_id)
        if count > 0:
            set_warns(guild.id, user_id, count - 1)
            return "one warning removed"
        return "no warnings left to remove"
    return "no automatic action for this sanction type — reviewed manually"


async def _dm_user_appeal_result(user_id, guild_name, case_id, accepted, result):
    user = await core.bot.fetch_user(user_id)
    if accepted:
        await user.send(f"✅ Your appeal for **{guild_name}** (case #{case_id}) was accepted — {result}.")
    else:
        await user.send(f"❌ Your appeal for **{guild_name}** (case #{case_id}) was reviewed and denied.")
