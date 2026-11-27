"""moderation_actions -- icônes de sanction et actions Discord liées aux appels.

Extrait de main.py (étapes 2b-1 et 2b-2a). Le bot est lu via `core.bot` (jamais `import main`).
"""
import discord

from nexus_runtime import core
from stores.config_store import get_config
from stores.sanctions_store import get_warns, set_warns

SANCTION_ICONS = {
    "ban": "🔨",
    "kick": "👢",
    "mute": "🔇",
    "warn": "⚠️",
    "softban": "🧹",
    "note": "📝",
    "automod_spam": "🤖",
    "automod_link": "🔗",
    "automod_link_global": "🌐",
    "automod_mention_spam": "📢",
    "automod_media_flood": "🖼️",
    "automod_doxx": "🪪",
    "automod_caps": "🔠",
}


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


async def post_appeal_for_review(guild, appeal, appeal_text):
    cfg = get_config(guild.id)
    channel_id = cfg.get("appeal_channel_id")
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    if channel is None:
        return
    sanction_type = appeal.get("sanction_type", "ban")
    icon = SANCTION_ICONS.get(sanction_type, "📨")
    embed = discord.Embed(title=f"{icon} Appeal — Case #{appeal['case_id']} ({sanction_type})", color=0xffb84d)
    embed.add_field(name="User", value=f"{appeal['user_name']} ({appeal['user_id']})", inline=False)
    embed.add_field(name="Original reason", value=appeal["ban_reason"][:1000] or "No reason provided", inline=False)
    embed.add_field(name="Appeal", value=appeal_text[:1000], inline=False)
    embed.set_footer(text=f"Review with: /appeal accept case_id:{appeal['case_id']}  or  /appeal deny case_id:{appeal['case_id']}")
    await channel.send(embed=embed)
