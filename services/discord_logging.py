"""discord_logging -- envoi des embeds de logs dans le salon de logs du serveur.

Extrait de main.py (étape 2b-1). Passe par un webhook (créé et mis en cache
automatiquement), avec repli sur un envoi classique. Le bot est lu via
`core.bot` (jamais `import main`).
"""
import discord

import core
from config_store import get_config, update_config


async def get_or_create_log_webhook(guild, channel):
    """Récupère le webhook de logs en cache pour ce salon, ou en crée un.
    Un webhook n'a pas besoin que le bot ait la permission d'envoyer des
    messages classiques dans ce salon — juste de l'avoir créé une fois."""
    cfg = get_config(guild.id)
    webhook_url = cfg.get("log_webhook_url")
    webhook_channel_id = cfg.get("log_webhook_channel_id")
    if webhook_url and webhook_channel_id == str(channel.id):
        try:
            return discord.Webhook.from_url(webhook_url, client=core.bot)
        except (discord.errors.InvalidData, ValueError):
            pass  # webhook invalide (supprimé manuellement ?), on en recrée un plus bas

    try:
        webhook = await channel.create_webhook(name="Nexus Logs", reason="Nexus log webhook")
        update_config(guild.id, "log_webhook_url", webhook.url)
        update_config(guild.id, "log_webhook_channel_id", str(channel.id))
        return webhook
    except discord.HTTPException as e:
        print(f"[WEBHOOK] Failed to create log webhook in guild {guild.id}: {e}", flush=True)
        return None


async def _send_log_embed(guild, embed):
    """Envoie un embed dans le channel de logs configuré pour ce serveur,
    via un webhook (créé/mis en cache automatiquement) plutôt qu'un message
    classique du bot — avec repli sur l'envoi classique si le webhook
    échoue ou ne peut pas être créé (ex: permission Manage Webhooks manquante)."""
    cfg = get_config(guild.id)
    channel = discord.utils.get(guild.text_channels, name=cfg.get("logs_channel", "logs"))
    if channel is None:
        return
    webhook = await get_or_create_log_webhook(guild, channel)
    if webhook is not None:
        try:
            await webhook.send(embed=embed, username="Nexus Logs")
            return
        except discord.HTTPException as e:
            print(f"[WEBHOOK] Failed to send via webhook, falling back to normal send: {e}", flush=True)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


def log_dashboard_action(guild, actor_name, action, details=""):
    """Poste un embed dans le salon de logs Discord quand une action est
    faite depuis le dashboard web — seulement si l'option est activée
    (config.log_dashboard_actions)."""
    cfg = get_config(guild.id)
    if not cfg.get("log_dashboard_actions"):
        return None
    embed = discord.Embed(title="🖥️ Dashboard change", color=0x9b59b6)
    embed.add_field(name="By", value=actor_name, inline=True)
    embed.add_field(name="Action", value=action, inline=True)
    if details:
        embed.add_field(name="Details", value=details[:1000], inline=False)
    return _send_log_embed(guild, embed)


def log_ban(guild, member, reason):
    embed = discord.Embed(title="🔨 Member Banned", color=0xff0000)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Banned by", value="📱 mobile app", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)


def log_kick(guild, member, reason):
    embed = discord.Embed(title="👢 Member Kicked", color=0xff0000)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Kicked by", value="📱 mobile app", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)


def log_mute(guild, member, minutes, reason):
    embed = discord.Embed(title="🔇 Member Muted", color=0xff6600)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Muted by", value="📱 mobile app", inline=True)
    embed.add_field(name="Duration", value=f"{minutes} minutes", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)


def log_unmute(guild, member):
    embed = discord.Embed(title="🔊 Member Unmuted", color=0xff6600)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Unmuted by", value="📱 mobile app", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)


def log_warn(guild, member, reason, count):
    embed = discord.Embed(title="⚠️ Member Warned", color=0xffcc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Warned by", value="📱 mobile app", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Warnings", value=f"{count}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)


def log_unwarn(guild, member, count):
    embed = discord.Embed(title="✅ Warning Removed", color=0xffcc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Unwarn by", value="📱 mobile app", inline=True)
    embed.add_field(name="Remaining Warnings", value=f"{count}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)
