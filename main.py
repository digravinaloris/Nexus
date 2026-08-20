import discord
from discord.ext import commands
from discord import app_commands
import os
from flask import Flask, request, jsonify, g, session, redirect, url_for, render_template_string, flash, get_flashed_messages
import requests
from threading import Thread
import asyncio
import datetime
import time
from pymongo import MongoClient
from functools import wraps
import yt_dlp
import re
import secrets
import subprocess
import sys

# MongoDB setup
mongo = None
db = None
warns_col = None
config_col = None
locked_channels_col = None
sanctions_col = None
reaction_roles_col = None
notes_col = None

def init_mongo():
    global mongo, db, warns_col, config_col, locked_channels_col, sanctions_col, reaction_roles_col, notes_col
    mongo = MongoClient(os.getenv("MONGO_URI"), serverSelectionTimeoutMS=5000)
    db = mongo["discordbot"]
    warns_col = db["warns"]
    config_col = db["config"]
    locked_channels_col = db["locked_channels"]
    sanctions_col = db["sanctions"]
    reaction_roles_col = db["reaction_roles"]
    notes_col = db["notes"]

def get_config(guild_id):
    doc = config_col.find_one({"guild_id": str(guild_id)})
    if not doc:
        doc = {
            "guild_id": str(guild_id),
            "logs_channel": "logs",
            "autorole": None,
            "allowed_roles": [],
            "command_roles": {}
        }
        config_col.insert_one(doc)
    return doc

def update_config(guild_id, key, value):
    config_col.update_one({"guild_id": str(guild_id)}, {"$set": {key: value}}, upsert=True)

def add_command_role(guild_id, command_name, role_id):
    """Autorise un rôle à utiliser une commande spécifique pour ce serveur."""
    config_col.update_one(
        {"guild_id": str(guild_id)},
        {"$addToSet": {f"command_roles.{command_name}": role_id}},
        upsert=True,
    )

def remove_command_role(guild_id, command_name, role_id):
    """Retire l'autorisation d'un rôle pour une commande spécifique sur ce serveur."""
    config_col.update_one(
        {"guild_id": str(guild_id)},
        {"$pull": {f"command_roles.{command_name}": role_id}},
        upsert=True,
    )

def get_warns(guild_id, user_id):
    doc = warns_col.find_one({"guild_id": str(guild_id), "user_id": str(user_id)})
    return doc["count"] if doc else 0

def set_warns(guild_id, user_id, count):
    warns_col.update_one({"guild_id": str(guild_id), "user_id": str(user_id)}, {"$set": {"count": count}}, upsert=True)

def mark_channel_locked(guild_id, channel_id, channel_name, locked_by, channel_type="text"):
    locked_channels_col.update_one(
        {"guild_id": str(guild_id), "channel_id": str(channel_id)},
        {"$set": {
            "channel_name": channel_name,
            "channel_type": channel_type,
            "locked_by": str(locked_by),
            "locked_at": datetime.datetime.utcnow(),
        }},
        upsert=True,
    )

def mark_channel_unlocked(guild_id, channel_id):
    locked_channels_col.delete_one({"guild_id": str(guild_id), "channel_id": str(channel_id)})

def get_locked_channels(guild_id):
    return list(locked_channels_col.find({"guild_id": str(guild_id)}))

def log_sanction(guild_id, user_id, sanction_type, reason, moderator_id):
    """Enregistre une sanction dans l'historique. moderator_id peut être un ID Discord,
    'mobile_app' (action via l'app Android), ou 'automod' (déclenchée automatiquement)."""
    sanctions_col.insert_one({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "type": sanction_type,
        "reason": reason or "No reason provided",
        "moderator_id": str(moderator_id),
        "timestamp": datetime.datetime.utcnow(),
    })

def get_sanction_history(guild_id, user_id, limit=15):
    return list(
        sanctions_col.find({"guild_id": str(guild_id), "user_id": str(user_id)})
        .sort("timestamp", -1)
        .limit(limit)
    )

def save_reaction_role(guild_id, message_id, channel_id, emoji, role_id):
    reaction_roles_col.update_one(
        {"guild_id": str(guild_id), "message_id": str(message_id), "emoji": str(emoji)},
        {"$set": {"channel_id": str(channel_id), "role_id": str(role_id)}},
        upsert=True,
    )

def get_reaction_role(guild_id, message_id, emoji):
    return reaction_roles_col.find_one({
        "guild_id": str(guild_id),
        "message_id": str(message_id),
        "emoji": str(emoji),
    })

def delete_reaction_roles(guild_id, message_id):
    reaction_roles_col.delete_many({"guild_id": str(guild_id), "message_id": str(message_id)})

# Tempbans actifs : {(guild_id, user_id): timestamp_unban}
# Stocké en mémoire + DB pour survivre aux redémarrages
def save_tempban(guild_id, user_id, unban_at):
    sanctions_col.update_one(
        {"guild_id": str(guild_id), "user_id": str(user_id), "type": "tempban_active"},
        {"$set": {"unban_at": unban_at}},
        upsert=True,
    )

def remove_tempban(guild_id, user_id):
    sanctions_col.delete_one({"guild_id": str(guild_id), "user_id": str(user_id), "type": "tempban_active"})

def get_active_tempbans():
    return list(sanctions_col.find({"type": "tempban_active"}))

# Bot setup
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
bot.locked_guilds = set()  # {guild_id, ...} — serveurs actuellement verrouillés (par serveur, pas global)
bot.start_time = time.time()
bot.ready_event = None  # set in on_ready, used so Flask waits until bot is ready

API_KEY = os.getenv("API_KEY")  # clé secrète pour protéger l'API, à définir sur Render

def has_admin():
    """Decorator: réservé aux membres avec la permission Discord Administrator."""
    async def predicate(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(description="❌ You need Administrator permission to use this command.", color=0xff0000)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

def has_owner():
    """Decorator: réservé au owner du serveur (interaction.guild.owner_id), même les admins ne passent pas."""
    async def predicate(interaction: discord.Interaction):
        if interaction.user.id != interaction.guild.owner_id:
            embed = discord.Embed(description="❌ Only the server owner can use this command.", color=0xff0000)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

async def check_access(interaction: discord.Interaction, command_name: str, native_permission: str = None) -> bool:
    """Vérifie si l'utilisateur peut utiliser une commande donnée.
    - Si CE serveur est verrouillé, seuls les admins passent.
    - Les administrateurs du serveur passent toujours.
    - Si des rôles ont été configurés pour cette commande via /config allow, seuls ces rôles (ou un admin) peuvent l'utiliser.
    - Sinon, retombe sur la permission Discord native fournie (ou ouvert à tous si aucune n'est fournie).
    """
    if interaction.guild_id in bot.locked_guilds and not interaction.user.guild_permissions.administrator:
        embed = discord.Embed(description="🔒 The bot is currently locked on this server.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    cfg = get_config(interaction.guild_id)
    allowed_roles = set(cfg.get("command_roles", {}).get(command_name, []))
    if allowed_roles:
        user_roles = {role.id for role in interaction.user.roles}
        if user_roles & allowed_roles:
            return True
        embed = discord.Embed(description="❌ You don't have permission to use this command.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return False
    if native_permission is None:
        return True
    if getattr(interaction.user.guild_permissions, native_permission, False):
        return True
    embed = discord.Embed(description="❌ You don't have permission to use this command.", color=0xff0000)
    await interaction.response.send_message(embed=embed)
    return False

@bot.event
async def on_ready():
    init_mongo()
    try:
        await bot.tree.sync()
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Sync error: {e}")
    print(f"{bot.user} is online!")
    bot.loop.create_task(tempban_check_loop())

@bot.event
async def on_guild_join(guild: discord.Guild):
    """Quand le bot rejoint un nouveau serveur : crée sa config par défaut et prévient le owner."""
    get_config(guild.id)  # crée le document de config par défaut pour ce serveur
    try:
        owner = guild.owner or await guild.fetch_owner()
        embed = discord.Embed(
            title="👋 Thanks for adding Jello Bello!",
            description=(
                f"I'm now active on **{guild.name}**. Here's how to get started:\n\n"
                "• `/config logs <channel>` — set where logs are sent\n"
                "• `/config autorole <role>` — role given automatically to new members\n"
                "• `/config allow <command> <role>` / `/config disallow` — let a specific role use a specific command "
                "(otherwise the default Discord permission is used, e.g. Ban Members for `/ban`) — **server owner only**\n"
                "• `/config apikey` — generate an API key if you want to use the mobile companion app\n"
                "• `/botlock` / `/botunlock` — lock/unlock the bot on this server — **server owner only**\n\n"
                "Most `/config` commands require the **Administrator** permission, except `allow`/`disallow` which are owner-only. Have fun! 🎉"
            ),
            color=0x3399ff,
        )
        await owner.send(embed=embed)
    except Exception as e:
        print(f"on_guild_join welcome DM failed: {e}")

@bot.event
async def on_guild_remove(guild: discord.Guild):
    """Quand le bot est retiré d'un serveur : nettoie toutes les données stockées pour ce serveur."""
    guild_id = str(guild.id)
    try:
        config_col.delete_one({"guild_id": guild_id})
        warns_col.delete_many({"guild_id": guild_id})
        locked_channels_col.delete_many({"guild_id": guild_id})
        sanctions_col.delete_many({"guild_id": guild_id})
        reaction_roles_col.delete_many({"guild_id": guild_id})
        notes_col.delete_many({"guild_id": guild_id})
        print(f"Cleaned up data for removed guild {guild_id}")
    except Exception as e:
        print(f"on_guild_remove cleanup failed: {e}")

# /ban
@bot.tree.command(name="ban", description="Ban a member")
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not await check_access(interaction, "ban", "ban_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't ban this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    try:
        await member.ban(reason=reason)
        log_sanction(interaction.guild_id, member.id, "ban", reason, interaction.user.id)
        embed = discord.Embed(title="🔨 Member Banned", color=0xff0000)
        embed.add_field(name="User", value=f"**{member}**", inline=True)
        embed.add_field(name="Banned by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    except:
        embed = discord.Embed(description="❌ I can't ban this member.", color=0xff0000)
        await interaction.response.send_message(embed=embed)

# /kick
@bot.tree.command(name="kick", description="Kick a member")
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not await check_access(interaction, "kick", "kick_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't kick this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    try:
        await member.kick(reason=reason)
        log_sanction(interaction.guild_id, member.id, "kick", reason, interaction.user.id)
        embed = discord.Embed(title="👢 Member Kicked", color=0xff0000)
        embed.add_field(name="User", value=f"**{member}**", inline=True)
        embed.add_field(name="Kicked by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    except:
        embed = discord.Embed(description="❌ I can't kick this member.", color=0xff0000)
        await interaction.response.send_message(embed=embed)

# /mute
@bot.tree.command(name="mute", description="Timeout a member")
async def mute(interaction: discord.Interaction, member: discord.Member, minutes: int = 10, reason: str = "No reason provided"):
    if not await check_access(interaction, "mute", "moderate_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't mute this member, their role is too high.", color=0xff6600)
        await interaction.response.send_message(embed=embed)
        return
    try:
        duration = datetime.timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
        log_sanction(interaction.guild_id, member.id, "mute", f"{reason} ({minutes} min)", interaction.user.id)
        embed = discord.Embed(title="🔇 Member Muted", color=0xff6600)
        embed.add_field(name="User", value=f"**{member}**", inline=True)
        embed.add_field(name="Muted by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
        embed.add_field(name="Duration", value=f"{minutes} minutes", inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    except:
        embed = discord.Embed(description="❌ I can't mute this member.", color=0xff6600)
        await interaction.response.send_message(embed=embed)

# /unmute
@bot.tree.command(name="unmute", description="Remove timeout from a member")
async def unmute(interaction: discord.Interaction, member: discord.Member):
    if not await check_access(interaction, "unmute", "moderate_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't unmute this member, their role is too high.", color=0xff6600)
        await interaction.response.send_message(embed=embed)
        return
    await member.timeout(None)
    embed = discord.Embed(title="🔊 Member Unmuted", color=0xff6600)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Unmuted by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# /unban
@bot.tree.command(name="unban", description="Unban a user by ID")
async def unban(interaction: discord.Interaction, user_id: str):
    if not await check_access(interaction, "unban", "ban_members"): return
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        embed = discord.Embed(title="✅ Member Unbanned", color=0x00cc00)
        embed.add_field(name="User", value=f"**{user}**", inline=True)
        embed.add_field(name="Unbanned by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    except:
        embed = discord.Embed(description="❌ User not found or not banned.", color=0xff0000)
        await interaction.response.send_message(embed=embed)

# /warn
@bot.tree.command(name="warn", description="Warn a member")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not await check_access(interaction, "warn", "manage_messages"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't warn this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    await interaction.response.defer()
    count = get_warns(interaction.guild_id, member.id) + 1
    set_warns(interaction.guild_id, member.id, count)
    log_sanction(interaction.guild_id, member.id, "warn", reason, interaction.user.id)
    embed = discord.Embed(title="⚠️ Member Warned", color=0xffcc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Warned by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Warnings", value=f"{count}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.followup.send(embed=embed)

# /unwarn
@bot.tree.command(name="unwarn", description="Remove a warning from a member")
async def unwarn(interaction: discord.Interaction, member: discord.Member):
    if not await check_access(interaction, "unwarn", "manage_messages"): return
    await interaction.response.defer()
    count = get_warns(interaction.guild_id, member.id)
    if count == 0:
        embed = discord.Embed(description=f"❌ **{member}** has no warnings.", color=0xff0000)
        await interaction.followup.send(embed=embed)
        return
    count -= 1
    set_warns(interaction.guild_id, member.id, count)
    embed = discord.Embed(title="✅ Warning Removed", color=0xffcc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Unwarn by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.add_field(name="Remaining Warnings", value=f"{count}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.followup.send(embed=embed)

# /warnings
@bot.tree.command(name="warnings", description="Check warnings of a member")
async def warnings(interaction: discord.Interaction, member: discord.Member):
    if not await check_access(interaction, "warnings", None): return
    await interaction.response.defer()
    count = get_warns(interaction.guild_id, member.id)
    embed = discord.Embed(title="📋 Warnings", color=0xffcc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Total Warnings", value=f"{count}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.followup.send(embed=embed)

# /clear
@bot.tree.command(name="clear", description="Cleans messages from a channel")
@app_commands.describe(
    amount="Number of messages to scan",
    filter_by_user="Only delete messages from this user",
    filter_by_role="Only delete messages from members with this role",
    filter_by_bots="Only delete messages sent by bots",
)
async def clear(
    interaction: discord.Interaction,
    amount: int = 10,
    filter_by_user: discord.Member = None,
    filter_by_role: discord.Role = None,
    filter_by_bots: bool = False,
):
    if not await check_access(interaction, "clear", "manage_messages"): return

    def message_check(message):
        if filter_by_user and message.author.id != filter_by_user.id:
            return False
        if filter_by_role and (not isinstance(message.author, discord.Member) or filter_by_role not in message.author.roles):
            return False
        if filter_by_bots and not message.author.bot:
            return False
        return True

    filters_active = filter_by_user or filter_by_role or filter_by_bots
    desc = f"🗑️ Clearing **{amount}** messages"
    if filter_by_user:
        desc += f" from {filter_by_user.mention}"
    if filter_by_role:
        desc += f" with role {filter_by_role.mention}"
    if filter_by_bots:
        desc += " sent by bots"
    desc += "..."

    embed = discord.Embed(description=desc, color=0x3399ff)
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(2)

    if filters_active:
        deleted = await interaction.channel.purge(limit=amount, check=message_check)
        result_embed = discord.Embed(description=f"✅ Deleted **{len(deleted)}** matching message(s).", color=0x00cc00)
        await interaction.channel.send(embed=result_embed)
    else:
        await interaction.channel.purge(limit=amount + 1)

# /mutelist
@bot.tree.command(name="mutelist", description="List all muted members")
async def mutelist(interaction: discord.Interaction):
    if not await check_access(interaction, "mutelist", None): return
    muted = [m for m in interaction.guild.members if m.is_timed_out()]
    if not muted:
        embed = discord.Embed(description="✅ No members are currently muted.", color=0x00cc00)
        await interaction.response.send_message(embed=embed)
        return
    embed = discord.Embed(title="🔇 Muted Members", color=0xff6600)
    for m in muted:
        until = m.timed_out_until.strftime("%Y-%m-%d %H:%M UTC")
        embed.add_field(name=f"{m}", value=f"Until: {until}", inline=False)
    await interaction.response.send_message(embed=embed)

# /roleadd
@bot.tree.command(name="roleadd", description="Give a role to a member")
async def roleadd(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if not await check_access(interaction, "roleadd", "manage_roles"): return
    if role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't give this role, it's too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    if role in member.roles:
        embed = discord.Embed(description=f"❌ **{member}** already has the role {role.mention}.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    await member.add_roles(role)
    embed = discord.Embed(title="✅ Role Added", color=0x00cc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Role", value=role.mention, inline=True)
    embed.add_field(name="Added by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# /roleremove
@bot.tree.command(name="roleremove", description="Remove a role from a member")
async def roleremove(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if not await check_access(interaction, "roleremove", "manage_roles"): return
    if role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't remove this role, it's too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    if role not in member.roles:
        embed = discord.Embed(description=f"❌ **{member}** doesn't have the role {role.mention}.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    await member.remove_roles(role)
    embed = discord.Embed(title="✅ Role Removed", color=0x00cc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Role", value=role.mention, inline=True)
    embed.add_field(name="Removed by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# /lock
@bot.tree.command(name="lock", description="Lock a channel")
async def lock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not await check_access(interaction, "lock", "manage_channels"): return
    channel = channel or interaction.channel
    await channel.set_permissions(interaction.guild.default_role, send_messages=False)
    mark_channel_locked(interaction.guild_id, channel.id, channel.name, interaction.user.id)
    embed = discord.Embed(title="🔒 Channel Locked", color=0xff0000)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.add_field(name="Locked by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    await interaction.response.send_message(embed=embed)

# /unlock
@bot.tree.command(name="unlock", description="Unlock a channel")
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not await check_access(interaction, "unlock", "manage_channels"): return
    channel = channel or interaction.channel
    await channel.set_permissions(interaction.guild.default_role, send_messages=True)
    mark_channel_unlocked(interaction.guild_id, channel.id)
    embed = discord.Embed(title="🔓 Channel Unlocked", color=0x00cc00)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.add_field(name="Unlocked by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    await interaction.response.send_message(embed=embed)

# /vlock
@bot.tree.command(name="vlock", description="Lock a voice channel (prevent members from connecting)")
async def vlock(interaction: discord.Interaction, channel: discord.VoiceChannel = None):
    if not await check_access(interaction, "vlock", "manage_channels"): return
    channel = channel or (interaction.user.voice.channel if interaction.user.voice else None)
    if channel is None:
        embed = discord.Embed(description="❌ Specify a voice channel or join one first.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await channel.set_permissions(interaction.guild.default_role, connect=False)
    mark_channel_locked(interaction.guild_id, channel.id, channel.name, interaction.user.id, channel_type="voice")

    # Déconnecte tous les membres déjà présents dans le salon au moment du lock
    disconnected = []
    for member in channel.members:
        try:
            await member.move_to(None)
            disconnected.append(member)
        except Exception as e:
            print(f"Failed to disconnect {member} from voice channel: {e}")

    embed = discord.Embed(title="🔒 Voice Channel Locked", color=0xff0000)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.add_field(name="Locked by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    if disconnected:
        embed.add_field(name="Disconnected", value=", ".join(m.mention for m in disconnected), inline=False)
    await interaction.response.send_message(embed=embed)

# /vunlock
@bot.tree.command(name="vunlock", description="Unlock a voice channel")
async def vunlock(interaction: discord.Interaction, channel: discord.VoiceChannel = None):
    if not await check_access(interaction, "vunlock", "manage_channels"): return
    channel = channel or (interaction.user.voice.channel if interaction.user.voice else None)
    if channel is None:
        embed = discord.Embed(description="❌ Specify a voice channel or join one first.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await channel.set_permissions(interaction.guild.default_role, connect=True)
    mark_channel_unlocked(interaction.guild_id, channel.id)
    embed = discord.Embed(title="🔓 Voice Channel Unlocked", color=0x00cc00)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.add_field(name="Unlocked by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    await interaction.response.send_message(embed=embed)

# /lockedchannels
@bot.tree.command(name="lockedchannels", description="List all currently locked channels")
async def lockedchannels(interaction: discord.Interaction):
    if not await check_access(interaction, "lockedchannels", None): return
    locked = get_locked_channels(interaction.guild_id)
    if not locked:
        embed = discord.Embed(description="✅ No channels are currently locked.", color=0x00cc00)
        await interaction.response.send_message(embed=embed)
        return
    embed = discord.Embed(title="🔒 Locked Channels", color=0xff0000)
    for doc in locked:
        channel_id = doc["channel_id"]
        channel_name = doc.get("channel_name", "unknown")
        channel_type = doc.get("channel_type", "text")
        icon = "🔊" if channel_type == "voice" else "#"
        locked_at = doc.get("locked_at")
        locked_at_str = locked_at.strftime("%Y-%m-%d %H:%M UTC") if locked_at else "Unknown"
        try:
            locked_by_user = await bot.fetch_user(int(doc.get("locked_by")))
            locked_by_str = str(locked_by_user)
        except Exception:
            locked_by_str = f"ID {doc.get('locked_by', 'unknown')}"
        embed.add_field(
            name=f"{icon} {channel_name}",
            value=f"<#{channel_id}>\nLocked by **{locked_by_str}** on {locked_at_str}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="slowmode", description="Set slowmode delay on a channel")
@app_commands.describe(channel="The channel to apply slowmode to", seconds="Delay in seconds (0 to disable, max 21600)")
async def slowmode(interaction: discord.Interaction, channel: discord.TextChannel, seconds: int):
    if not await check_access(interaction, "slowmode", "manage_channels"): return
    seconds = max(0, min(seconds, 21600))
    await channel.edit(slowmode_delay=seconds)
    if seconds == 0:
        embed = discord.Embed(description=f"✅ Slowmode disabled in {channel.mention}.", color=0x00cc00)
    else:
        embed = discord.Embed(description=f"✅ Slowmode set to **{seconds}s** in {channel.mention}.", color=0x00cc00)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nickname", description="Change a member's nickname")
@app_commands.describe(member="The member to rename", nickname="New nickname (leave empty to reset)")
async def nickname(interaction: discord.Interaction, member: discord.Member, nickname: str = None):
    if not await check_access(interaction, "nickname", "manage_nicknames"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't rename this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    try:
        await member.edit(nick=nickname)
    except Exception as e:
        embed = discord.Embed(description=f"❌ Error: {e}", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if nickname:
        embed = discord.Embed(description=f"✅ {member.mention}'s nickname changed to **{nickname}**.", color=0x00cc00)
    else:
        embed = discord.Embed(description=f"✅ {member.mention}'s nickname reset.", color=0x00cc00)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="groupnickname", description="Add or remove a prefix on the nickname of every member with a role")
@app_commands.describe(role="The role to target", prefix="The prefix to add (e.g. '[EVENT]', a space is added automatically)", remove="Remove this prefix instead of adding it")
async def groupnickname(interaction: discord.Interaction, role: discord.Role, prefix: str, remove: bool = False):
    if not await check_access(interaction, "groupnickname", "manage_nicknames"): return
    if role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't manage nicknames for this role, it's too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.defer()
    updated, skipped = 0, 0
    for member in role.members:
        if member.top_role >= interaction.guild.me.top_role:
            skipped += 1
            continue
        current = member.nick or member.name
        full_prefix = f"{prefix} "
        try:
            if remove:
                if current.startswith(full_prefix):
                    new_nick = current[len(full_prefix):] or None
                    await member.edit(nick=new_nick)
                    updated += 1
            else:
                if not current.startswith(full_prefix):
                    new_nick = (full_prefix + current)[:32]
                    await member.edit(nick=new_nick)
                    updated += 1
        except Exception:
            skipped += 1
    action = "removed from" if remove else "added to"
    embed = discord.Embed(
        description=f"✅ Prefix **{action}** {updated} member(s) with {role.mention}." + (f" ({skipped} skipped)" if skipped else ""),
        color=0x00cc00,
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="warnlist", description="List all warned members")
async def warnlist(interaction: discord.Interaction):
    if not await check_access(interaction, "warnlist", "manage_messages"): return
    await interaction.response.defer()
    docs = warns_col.find({"guild_id": str(interaction.guild_id), "count": {"$gt": 0}})
    warned = list(docs)
    if not warned:
        embed = discord.Embed(description="✅ No members have warnings.", color=0x00cc00)
        await interaction.followup.send(embed=embed)
        return
    embed = discord.Embed(title="⚠️ Warned Members", color=0xffcc00)
    for doc in warned:
        try:
            user = await bot.fetch_user(int(doc["user_id"]))
            embed.add_field(name=f"{user}", value=f"{doc['count']} warning(s)", inline=False)
        except:
            embed.add_field(name=f"Unknown ({doc['user_id']})", value=f"{doc['count']} warning(s)", inline=False)
    await interaction.followup.send(embed=embed)

# /history
SANCTION_ICONS = {
    "ban": "🔨",
    "kick": "👢",
    "mute": "🔇",
    "warn": "⚠️",
    "softban": "🧹",
    "note": "📝",
    "automod_spam": "🤖",
    "automod_link": "🔗",
    "automod_caps": "🔠",
}

@bot.tree.command(name="history", description="Show moderation history for a member")
async def history(interaction: discord.Interaction, member: discord.Member):
    if not await check_access(interaction, "history", None): return
    await interaction.response.defer()
    records = get_sanction_history(interaction.guild_id, member.id)
    if not records:
        embed = discord.Embed(description=f"✅ No sanctions found for **{member}**.", color=0x00cc00)
        await interaction.followup.send(embed=embed)
        return

    embed = discord.Embed(title=f"📋 Sanction History — {member}", color=0x3399ff)
    embed.set_thumbnail(url=member.display_avatar.url)
    for record in records:
        icon = SANCTION_ICONS.get(record["type"], "•")
        date_str = record["timestamp"].strftime("%Y-%m-%d %H:%M UTC")
        mod_id = record.get("moderator_id", "")
        if mod_id == "mobile_app":
            mod_str = "📱 Mobile App"
        elif mod_id == "automod":
            mod_str = "🤖 AutoMod"
        else:
            try:
                mod_user = await bot.fetch_user(int(mod_id))
                mod_str = str(mod_user)
            except Exception:
                mod_str = f"ID {mod_id}"
        embed.add_field(
            name=f"{icon} {record['type'].replace('_', ' ').title()} — {date_str}",
            value=f"Reason: {record['reason']}\nBy: {mod_str}",
            inline=False,
        )
    if len(records) >= 15:
        embed.set_footer(text="Showing the 15 most recent sanctions")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="note", description="Add an internal staff note on a member (not visible to them)")
@app_commands.describe(member="The member to note", text="The note content")
async def note(interaction: discord.Interaction, member: discord.Member, text: str):
    if not await check_access(interaction, "note", "manage_messages"): return
    notes_col.insert_one({
        "guild_id": str(interaction.guild_id),
        "user_id": str(member.id),
        "text": text,
        "moderator_id": str(interaction.user.id),
        "timestamp": datetime.datetime.utcnow(),
    })
    log_sanction(interaction.guild_id, member.id, "note", text, interaction.user.id)
    embed = discord.Embed(description=f"📝 Note added for **{member}**.", color=0x3399ff)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="notes", description="View internal staff notes for a member")
@app_commands.describe(member="The member whose notes to view")
async def notes(interaction: discord.Interaction, member: discord.Member):
    if not await check_access(interaction, "notes", "manage_messages"): return
    docs = list(notes_col.find({"guild_id": str(interaction.guild_id), "user_id": str(member.id)}).sort("timestamp", -1).limit(15))
    if not docs:
        embed = discord.Embed(description=f"✅ No notes for **{member}**.", color=0x00cc00)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    embed = discord.Embed(title=f"📝 Notes — {member}", color=0x3399ff)
    embed.set_thumbnail(url=member.display_avatar.url)
    for doc in docs:
        date_str = doc["timestamp"].strftime("%Y-%m-%d %H:%M UTC")
        try:
            mod = await bot.fetch_user(int(doc["moderator_id"]))
            mod_str = str(mod)
        except Exception:
            mod_str = f"ID {doc['moderator_id']}"
        embed.add_field(name=f"{date_str} — by {mod_str}", value=doc["text"], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="softban", description="Kick a member and delete their recent messages")
@app_commands.describe(member="The member to softban", reason="Reason", delete_days="Days of messages to delete (0-7)")
async def softban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: int = 1):
    if not await check_access(interaction, "softban", "ban_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't softban this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    delete_days = max(0, min(delete_days, 7))
    try:
        await member.ban(reason=f"[Softban] {reason}", delete_message_days=delete_days)
        await interaction.guild.unban(member, reason="Softban — automatic unban")
        log_sanction(interaction.guild_id, member.id, "softban", reason, interaction.user.id)
        embed = discord.Embed(title="🧹 Member Softbanned", color=0xff6600)
        embed.add_field(name="User", value=f"**{member}**", inline=True)
        embed.add_field(name="By", value=f"**{interaction.user}**", inline=True)
        embed.add_field(name="Messages deleted", value=f"Last {delete_days} day(s)", inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        embed = discord.Embed(description=f"❌ I can't softban this member: {e}", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="purgeuser", description="Delete all stored data (warnings, sanctions, notes) for a member on this server")
@app_commands.describe(user_id="The Discord user ID whose data to erase")
@has_admin()
async def purgeuser(interaction: discord.Interaction, user_id: str):
    guild_id = str(interaction.guild_id)
    warns_col.delete_many({"guild_id": guild_id, "user_id": user_id})
    sanctions_col.delete_many({"guild_id": guild_id, "user_id": user_id})
    notes_col.delete_many({"guild_id": guild_id, "user_id": user_id})
    embed = discord.Embed(
        description=f"🗑️ All stored data (warnings, sanctions, notes) for user ID `{user_id}` has been erased for this server.",
        color=0x00cc00,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# /banlist
@bot.tree.command(name="banlist", description="List all banned members")
async def banlist(interaction: discord.Interaction):
    if not await check_access(interaction, "banlist", "ban_members"): return
    await interaction.response.defer()
    bans = [entry async for entry in interaction.guild.bans()]
    if not bans:
        embed = discord.Embed(description="✅ No members are banned.", color=0x00cc00)
        await interaction.followup.send(embed=embed)
        return
    embed = discord.Embed(title="🔨 Banned Members", color=0xff0000)
    for entry in bans[:25]:
        embed.add_field(name=f"{entry.user}", value=f"Reason: {entry.reason or 'No reason'}", inline=False)
    await interaction.followup.send(embed=embed)

# /broadcast
@bot.tree.command(name="broadcast", description="Send a broadcast message")
async def broadcast(interaction: discord.Interaction, message: str, mention: str = "none"):
    if not await check_access(interaction, "broadcast", "manage_messages"): return
    if mention == "everyone":
        ping = "@everyone"
    elif mention == "here":
        ping = "@here"
    else:
        role = discord.utils.get(interaction.guild.roles, name=mention)
        ping = role.mention if role else ""
    embed = discord.Embed(description=message, color=0x3399ff)
    embed.set_footer(text=f"📢 Sent by {interaction.user.top_role.name} · {interaction.user.name}")
    confirm = discord.Embed(description="✅ Broadcast sent!", color=0x00cc00)
    await interaction.response.send_message(embed=confirm, ephemeral=True)
    await interaction.channel.send(content=ping if ping else None, embed=embed)

# ============================================================
# =======================  CONFIG  ===========================
# ============================================================

config_group = app_commands.Group(name="config", description="Configure the bot (Administrator only)")

@config_group.command(name="logs", description="Set the logs channel")
@has_admin()
async def config_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    update_config(interaction.guild_id, "logs_channel", channel.name)
    embed = discord.Embed(title="✅ Logs Channel Updated", description=f"Logs will now be sent to {channel.mention}", color=0x00cc00)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="autorole", description="Set the auto-join role")
@has_admin()
async def config_autorole(interaction: discord.Interaction, role: discord.Role):
    update_config(interaction.guild_id, "autorole", role.id)
    embed = discord.Embed(title="✅ Auto-Role Updated", description=f"New members will get {role.mention}", color=0x00cc00)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="allow", description="Allow a role to use a specific command (e.g. ban, kick, roleadd) — server owner only")
@app_commands.describe(command="Command name, without the slash (e.g. ban)", role="Role allowed to use it")
@has_owner()
async def config_allow(interaction: discord.Interaction, command: str, role: discord.Role):
    command = command.strip().lower().lstrip("/")
    add_command_role(interaction.guild_id, command, role.id)
    embed = discord.Embed(title="✅ Permission Added", description=f"{role.mention} can now use `/{command}`.", color=0x00cc00)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="disallow", description="Remove a role's permission for a specific command — server owner only")
@app_commands.describe(command="Command name, without the slash (e.g. ban)", role="Role to remove")
@has_owner()
async def config_disallow(interaction: discord.Interaction, command: str, role: discord.Role):
    command = command.strip().lower().lstrip("/")
    remove_command_role(interaction.guild_id, command, role.id)
    embed = discord.Embed(title="✅ Permission Removed", description=f"{role.mention} can no longer use `/{command}` (falls back to default Discord permissions).", color=0x00cc00)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="apikey", description="Generate a new API key for this server's mobile app access")
@has_admin()
async def config_apikey(interaction: discord.Interaction):
    new_key = secrets.token_hex(16)
    update_config(interaction.guild_id, "api_key", new_key)
    embed = discord.Embed(
        title="🔑 New API Key Generated",
        description=f"||{new_key}||\n\nUse this in the mobile app to manage **this server only**. Keep it secret — generating a new one invalidates the old one.",
        color=0x00cc00,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="view", description="View current config")
@has_admin()
async def config_view(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    autorole = f"<@&{cfg['autorole']}>" if cfg.get("autorole") else "None"
    embed = discord.Embed(title="⚙️ Server Config", color=0x3399ff)
    embed.add_field(name="Logs Channel", value=f"#{cfg.get('logs_channel', 'logs')}", inline=True)
    embed.add_field(name="Auto-Role", value=autorole, inline=True)
    embed.add_field(name="API Key", value="✅ Set (use `/config apikey` to regenerate)" if cfg.get("api_key") else "❌ Not set — use `/config apikey` to generate one", inline=True)
    embed.add_field(name="Locked", value="🔒 Yes" if interaction.guild_id in bot.locked_guilds else "🔓 No", inline=True)
    command_roles = cfg.get("command_roles", {})
    if command_roles:
        lines = []
        for cmd, roles in command_roles.items():
            if not roles:
                continue
            mentions = " ".join(f"<@&{r}>" for r in roles)
            lines.append(f"**/{cmd}** → {mentions}")
        embed.add_field(name="Command Permissions", value="\n".join(lines) or "None configured", inline=False)
    else:
        embed.add_field(name="Command Permissions", value="None configured (using default Discord permissions)", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

bot.tree.add_command(config_group)

@bot.tree.command(name="botlock", description="Lock the bot on this server (server owner only)")
@has_owner()
async def botlock(interaction: discord.Interaction):
    bot.locked_guilds.add(interaction.guild_id)
    embed = discord.Embed(description="🔒 Bot locked on this server. Only the server owner can use commands until `/botunlock`.", color=0xff0000)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="botunlock", description="Unlock the bot on this server (server owner only)")
@has_owner()
async def botunlock(interaction: discord.Interaction):
    bot.locked_guilds.discard(interaction.guild_id)
    embed = discord.Embed(description="🔓 Bot unlocked on this server.", color=0x00cc00)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================
# ===========  USERINFO / SERVERINFO / TEMPBAN / REACTION ROLES
# ============================================================

def parse_duration(duration_str: str) -> int:
    """Convertit une durée humaine ('1h', '2d', '30m', '1w') en secondes. Retourne -1 si invalide."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    match = re.fullmatch(r"(\d+)([smhdw])", duration_str.strip().lower())
    if not match:
        return -1
    return int(match.group(1)) * units[match.group(2)]


async def tempban_check_loop():
    """Tâche de fond qui lève les tempbans arrivés à expiration, toutes les 60 secondes."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = datetime.datetime.utcnow()
            for doc in get_active_tempbans():
                if doc["unban_at"] <= now:
                    guild = bot.get_guild(int(doc["guild_id"]))
                    if guild:
                        try:
                            user = await bot.fetch_user(int(doc["user_id"]))
                            await guild.unban(user, reason="Tempban expired")
                            remove_tempban(doc["guild_id"], doc["user_id"])
                            cfg = get_config(doc["guild_id"])
                            log_ch = discord.utils.get(guild.text_channels, name=cfg.get("logs_channel", "logs"))
                            if log_ch:
                                embed = discord.Embed(title="✅ Tempban Expired", color=0x00cc00)
                                embed.add_field(name="User", value=f"**{user}**", inline=True)
                                await log_ch.send(embed=embed)
                        except Exception as e:
                            print(f"Tempban unban error: {e}")
                            remove_tempban(doc["guild_id"], doc["user_id"])
        except Exception as e:
            print(f"Tempban loop error: {e}")
        await asyncio.sleep(60)


@bot.tree.command(name="userinfo", description="Show information about a member")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    roles = [r.mention for r in member.roles if not r.is_default()]
    roles_str = " ".join(roles) if roles else "None"
    joined_at = member.joined_at.strftime("%Y-%m-%d %H:%M UTC") if member.joined_at else "Unknown"
    created_at = member.created_at.strftime("%Y-%m-%d %H:%M UTC")
    status_icons = {
        discord.Status.online: "🟢 Online",
        discord.Status.idle: "🟡 Idle",
        discord.Status.dnd: "🔴 Do Not Disturb",
        discord.Status.offline: "⚫ Offline",
    }
    status = status_icons.get(member.status, "⚫ Offline")
    warn_count = get_warns(interaction.guild_id, member.id)
    embed = discord.Embed(title=f"👤 {member}", color=member.color if member.color.value else 0x3399ff)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=str(member.id), inline=True)
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
    embed.add_field(name="Joined Server", value=joined_at, inline=True)
    embed.add_field(name="Account Created", value=created_at, inline=True)
    embed.add_field(name="Warnings", value=str(warn_count), inline=True)
    embed.add_field(name=f"Roles ({len(roles)})", value=roles_str[:1024] if roles_str else "None", inline=False)
    embed.add_field(name="Top Role", value=member.top_role.mention if not member.top_role.is_default() else "None", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="serverinfo", description="Show information about the server")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    created_at = guild.created_at.strftime("%Y-%m-%d %H:%M UTC")
    text_channels = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    total_channels = text_channels + voice_channels
    bots = sum(1 for m in guild.members if m.bot)
    humans = guild.member_count - bots
    embed = discord.Embed(title=f"🌐 {guild.name}", color=0x3399ff)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="ID", value=str(guild.id), inline=True)
    embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="Created", value=created_at, inline=True)
    embed.add_field(name="Members", value=f"👥 {humans} humans · 🤖 {bots} bots", inline=True)
    embed.add_field(name="Channels", value=f"💬 {text_channels} text · 🔊 {voice_channels} voice", inline=True)
    embed.add_field(name="Roles", value=str(len(guild.roles) - 1), inline=True)
    embed.add_field(name="Boosts", value=f"⚡ {guild.premium_subscription_count} (Level {guild.premium_tier})", inline=True)
    embed.add_field(name="Verification Level", value=str(guild.verification_level).title(), inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="List all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Jello Bello — Commands", color=0x3399ff)
    embed.add_field(
        name="🛡️ Moderation",
        value="`/ban` `/unban` `/kick` `/mute` `/unmute` `/tempban` `/softban`\n"
              "`/warn` `/unwarn` `/warnings` `/warnlist` `/note` `/notes` `/history` `/banlist` `/mutelist` `/clear`",
        inline=False,
    )
    embed.add_field(
        name="🔒 Channels & Roles",
        value="`/lock` `/unlock` `/vlock` `/vunlock` `/lockedchannels`\n"
              "`/roleadd` `/roleremove` `/reactionrole` `/slowmode`\n"
              "`/nickname` `/groupnickname`",
        inline=False,
    )
    embed.add_field(
        name="📋 Applications & Utilities",
        value="`/apply` `/userinfo` `/serverinfo` `/broadcast` `/poll` `/botinfo` `/ping` `/help`",
        inline=False,
    )
    embed.add_field(
        name="🎵 Music",
        value="`/play` `/search` `/pause` `/resume` `/skip` `/stop` `/queue` `/volume`",
        inline=False,
    )
    embed.add_field(
        name="⚙️ Server Config",
        value="`/config logs` `/config autorole` `/config apikey` `/config view`\n"
              "`/config allow` `/config disallow` `/purgeuser` — owner/admin only\n"
              "`/botlock` `/botunlock` — server owner only",
        inline=False,
    )
    embed.set_footer(text="Full documentation on GitHub")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    embed = discord.Embed(description=f"🏓 Pong! `{latency_ms}ms`", color=0x00cc00)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="botinfo", description="Show information about the bot")
async def botinfo(interaction: discord.Interaction):
    uptime_seconds = int(time.time() - bot.start_time)
    days, rem = divmod(uptime_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    uptime_str = f"{days}d {hours}h {minutes}m"
    embed = discord.Embed(title="🤖 Jello Bello", color=0x3399ff)
    if bot.user.avatar:
        embed.set_thumbnail(url=bot.user.avatar.url)
    embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Library", value=f"discord.py {discord.__version__}", inline=True)
    embed.set_footer(text="github.com/digravinaloris/dc-bot")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="poll", description="Create a quick poll")
@app_commands.describe(
    question="The poll question",
    option1="First option (leave empty for a simple 👍/👎 poll)",
    option2="Second option",
    option3="Third option",
    option4="Fourth option",
)
async def poll(interaction: discord.Interaction, question: str, option1: str = None, option2: str = None, option3: str = None, option4: str = None):
    options = [o for o in [option1, option2, option3, option4] if o]
    embed = discord.Embed(title=f"📊 {question}", color=0x3399ff)
    embed.set_footer(text=f"Poll started by {interaction.user}")
    if options:
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
        embed.description = "\n".join(f"{number_emojis[i]} {opt}" for i, opt in enumerate(options))
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        for i in range(len(options)):
            await message.add_reaction(number_emojis[i])
    else:
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        await message.add_reaction("👍")
        await message.add_reaction("👎")



@bot.tree.command(name="tempban", description="Temporarily ban a member")
@app_commands.describe(duration="Duration: 30m, 2h, 1d, 1w")
async def tempban(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
    if not await check_access(interaction, "tempban", "ban_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't ban this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    seconds = parse_duration(duration)
    if seconds <= 0:
        embed = discord.Embed(description="❌ Invalid duration. Use `30m`, `2h`, `1d`, `1w`.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    unban_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    try:
        await member.ban(reason=f"[Tempban {duration}] {reason}")
        save_tempban(interaction.guild_id, member.id, unban_at)
        log_sanction(interaction.guild_id, member.id, "ban", f"[Tempban {duration}] {reason}", interaction.user.id)
        embed = discord.Embed(title="⏳ Member Tempbanned", color=0xff6600)
        embed.add_field(name="User", value=f"**{member}**", inline=True)
        embed.add_field(name="Duration", value=duration, inline=True)
        embed.add_field(name="Unbanned at", value=unban_at.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Banned by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        embed = discord.Embed(description=f"❌ Couldn't ban this member: {e}", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="reactionrole", description="Create a reaction role message")
@app_commands.describe(
    title="Title of the embed",
    channel="Channel where to post the embed",
    pairs="Emoji/role pairs separated by commas, e.g: 🎮 Gamer, 🎵 Music, 🎨 Art"
)
async def reactionrole(interaction: discord.Interaction, title: str, channel: discord.TextChannel, pairs: str):
    if not await check_access(interaction, "reactionrole", "manage_roles"): return
    await interaction.response.defer(ephemeral=True)

    # Parse les paires emoji/rôle depuis la string
    # Format attendu: "emoji RoleName, emoji RoleName, ..."
    parsed = []
    for pair in pairs.split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(None, 1)  # split sur le premier espace
        if len(parts) != 2:
            await interaction.followup.send(f"❌ Invalid pair: `{pair}` — expected `emoji RoleName`", ephemeral=True)
            return
        emoji_str, role_name = parts[0].strip(), parts[1].strip()
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if not role:
            await interaction.followup.send(f"❌ Role `{role_name}` not found.", ephemeral=True)
            return
        if role >= interaction.guild.me.top_role:
            await interaction.followup.send(f"❌ Role `{role_name}` is too high for me to manage.", ephemeral=True)
            return
        parsed.append((emoji_str, role))

    if not parsed:
        await interaction.followup.send("❌ No valid emoji/role pairs found.", ephemeral=True)
        return

    description = "\n".join(f"{emoji} — {role.mention}" for emoji, role in parsed)
    embed = discord.Embed(title=title, description=description, color=0x3399ff)
    embed.set_footer(text="React to get the corresponding role")

    try:
        msg = await channel.send(embed=embed)
        for emoji_str, role in parsed:
            try:
                await msg.add_reaction(emoji_str)
                save_reaction_role(interaction.guild_id, msg.id, channel.id, emoji_str, role.id)
            except discord.HTTPException:
                await interaction.followup.send(f"⚠️ Couldn't add reaction for `{emoji_str}` — make sure it's a valid emoji.", ephemeral=True)
        await interaction.followup.send(f"✅ Reaction role message created in {channel.mention}!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to create message: {e}", ephemeral=True)

# ============================================================
# ====================  APPLY SYSTEM  =======================
# ============================================================

class ApplicationModal(discord.ui.Modal):
    def __init__(self, role: discord.Role):
        super().__init__(title=f"Application for {role.name}")
        self.role = role
        self.question1 = discord.ui.TextInput(
            label="Why do you want this role?",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True,
        )
        self.question2 = discord.ui.TextInput(
            label="How old are you?",
            style=discord.TextStyle.short,
            max_length=50,
            required=True,
        )
        self.question3 = discord.ui.TextInput(
            label="What can you bring to the team?",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True,
        )
        self.add_item(self.question1)
        self.add_item(self.question2)
        self.add_item(self.question3)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "✅ Your application has been submitted! You'll be notified of the decision.", ephemeral=True
        )
        owner = interaction.guild.owner or await interaction.guild.fetch_owner()
        embed = discord.Embed(title=f"📋 New Application — {self.role.name}", color=0x3399ff)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Applicant", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        embed.add_field(name="Server", value=interaction.guild.name, inline=True)
        embed.add_field(name="Role", value=self.role.mention, inline=True)
        embed.add_field(name="Why this role?", value=self.question1.value, inline=False)
        embed.add_field(name="Age", value=self.question2.value, inline=True)
        embed.add_field(name="What they bring", value=self.question3.value, inline=False)
        view = ApplicationDecisionView(
            applicant_id=interaction.user.id,
            guild_id=interaction.guild_id,
            role_id=self.role.id,
        )
        try:
            await owner.send(embed=embed, view=view)
        except Exception as e:
            print(f"Apply DM error: {e}")


class ApplicationDecisionView(discord.ui.View):
    def __init__(self, applicant_id, guild_id, role_id):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.guild_id = guild_id
        self.role_id = role_id

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.green, custom_id="apply_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.client.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
            return
        member = guild.get_member(self.applicant_id)
        role = guild.get_role(self.role_id)
        if member and role:
            try:
                await member.add_roles(role, reason="Application accepted")
                try:
                    dm_embed = discord.Embed(
                        title="✅ Application Accepted!",
                        description=f"Your application for **{role.name}** on **{guild.name}** has been **accepted**. The role has been given to you!",
                        color=0x00cc00,
                    )
                    await member.send(embed=dm_embed)
                except Exception:
                    pass
                await interaction.response.send_message(f"✅ Accepted — {role.name} given to {member}.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Member or role not found.", ephemeral=True)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="❌ Refuse", style=discord.ButtonStyle.red, custom_id="apply_refuse")
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.client.get_guild(self.guild_id)
        if guild:
            member = guild.get_member(self.applicant_id)
            role = guild.get_role(self.role_id)
            if member:
                try:
                    dm_embed = discord.Embed(
                        title="❌ Application Refused",
                        description=f"Your application for **{role.name if role else 'the role'}** on **{guild.name}** has been **refused**.",
                        color=0xff0000,
                    )
                    await member.send(embed=dm_embed)
                except Exception:
                    pass
        await interaction.response.send_message("❌ Application refused.", ephemeral=True)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)


@bot.tree.command(name="apply", description="Apply for a role")
@app_commands.describe(role="The role you want to apply for")
async def apply(interaction: discord.Interaction, role: discord.Role):
    if not await check_access(interaction, "apply", None): return
    await interaction.response.send_modal(ApplicationModal(role=role))

# ============================================================
# =======================  MUSIC  ===========================
# ============================================================

# FFmpeg : Render (plan gratuit/standard) n'autorise pas apt-get (filesystem en lecture seule
# pour les paquets système), et le binaire statique téléchargé manuellement (johnvansickle.com)
# segfaultait (return code -11) -- probablement une incompatibilité d'architecture/glibc avec
# l'environnement Render. imageio-ffmpeg télécharge un binaire FFmpeg empaqueté spécifiquement
# pour fonctionner avec l'environnement Python détecté, ce qui est beaucoup plus fiable.
import imageio_ffmpeg
try:
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    print(f"[FFMPEG] Using imageio-ffmpeg binary at: {FFMPEG_PATH}")
    print(f"[FFMPEG] File exists: {os.path.isfile(FFMPEG_PATH)}, executable: {os.access(FFMPEG_PATH, os.X_OK)}")
except Exception as e:
    print(f"[FFMPEG] imageio_ffmpeg failed to provide a binary, falling back to system ffmpeg: {e}")
    FFMPEG_PATH = "ffmpeg"

# Cookies YouTube : Render bloque souvent les requêtes anonymes ("Sign in to confirm you're not a bot").
# On les fournit via une variable d'env (contenu du fichier cookies.txt exporté du navigateur) et on
# les réécrit sur disque au démarrage, car yt-dlp veut un vrai fichier.
YOUTUBE_COOKIES_CONTENT = os.getenv("YOUTUBE_COOKIES")
YOUTUBE_COOKIES_PATH = "/tmp/youtube_cookies.txt"

if YOUTUBE_COOKIES_CONTENT:
    with open(YOUTUBE_COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write(YOUTUBE_COOKIES_CONTENT)

DOWNLOAD_DIR = "/tmp/music_cache"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

YTDL_OPTIONS = {
    # bestaudio en priorité, puis n'importe quel format jouable en dernier recours
    # (FFmpeg extraira la piste audio même d'un format vidéo+audio combiné).
    "format": "bestaudio/best/bv*+ba/b",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "ffmpeg_location": FFMPEG_PATH,
    # On télécharge et convertit en mp3 localement plutôt que de streamer l'URL YouTube en
    # direct : plus stable, évite les soucis de protocole réseau qui faisaient planter FFmpeg
    # pendant le streaming live (segfault -11 observé avec le streaming direct).
    "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }],
    # YouTube exige de plus en plus un "PO Token" pour les clients android/ios/web, qu'on n'a
    # pas configuré -- ça cause "Requested format is not available". La sélection automatique
    # de yt-dlp est en fait la plus fiable actuellement : avec des cookies valides, elle choisit
    # le client "tv_downgraded" qui ne nécessite pas de PO Token. On ajoute juste web_embedded
    # en complément (recommandation officielle yt-dlp, issue #15847, fév. 2026).
    "extractor_args": {
        "youtube": {
            "player_client": ["default", "web_embedded"],
        }
    },
}
if YOUTUBE_COOKIES_CONTENT:
    YTDL_OPTIONS["cookiefile"] = YOUTUBE_COOKIES_PATH

# Fichier local mp3 déjà téléchargé : pas besoin de -reconnect (plus de réseau pendant la lecture).
FFMPEG_OPTIONS_TEMPLATE = {
    "before_options": "",
    "options": "-vn -af volume={volume}",
}

DEFAULT_VOLUME = 0.5  # 50%, ajustable par /volume (0.0 à 2.0)

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]+)")
SPOTIFY_PLAYLIST_RE = re.compile(r"open\.spotify\.com/(playlist|album)/([A-Za-z0-9]+)")


class Track:
    def __init__(self, title, filepath, webpage_url, duration, requester):
        self.title = title
        self.filepath = filepath  # chemin local du mp3 téléchargé
        self.webpage_url = webpage_url  # lien à afficher
        self.duration = duration
        self.requester = requester


class GuildMusicState:
    """Garde la queue et le lecteur vocal pour un serveur donné."""
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.queue = []
        self.voice_client = None
        self.current = None
        self.loop_lock = asyncio.Lock()
        self.volume = DEFAULT_VOLUME  # 0.0 à 2.0, ajustable via /volume

    def is_playing(self):
        return self.voice_client is not None and self.voice_client.is_playing()


music_states = {}  # {guild_id: GuildMusicState}


def get_music_state(guild_id):
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState(guild_id)
    return music_states[guild_id]


async def resolve_query(query, requester):
    """
    Transforme une recherche texte ou un lien YouTube/SoundCloud/Spotify en Track jouable.
    Pour Spotify, on récupère le titre/artiste via oEmbed (pas besoin de clé API) et on recherche sur YouTube.
    """
    loop = asyncio.get_event_loop()

    spotify_match = SPOTIFY_TRACK_RE.search(query)
    if spotify_match:
        # Spotify ne permet pas le streaming direct (DRM) : on récupère le titre via oEmbed et on recherche sur YouTube
        try:
            import urllib.request
            import json as jsonlib
            oembed_url = f"https://open.spotify.com/oembed?url={query}"
            with urllib.request.urlopen(oembed_url, timeout=5) as resp:
                data = jsonlib.loads(resp.read().decode())
            title = data.get("title", "")
            query = f"ytsearch:{title}"
        except Exception:
            query = f"ytsearch:{query}"
    elif query.startswith("http"):
        pass  # lien direct YouTube/SoundCloud, yt-dlp gère nativement
    else:
        query = f"ytsearch:{query}"

    def extract():
        # download=True : on télécharge réellement le fichier, le post-processeur le convertit en mp3
        info = ytdl.extract_info(query, download=True)
        if "entries" in info:
            info = info["entries"][0]
        return info

    info = await loop.run_in_executor(None, extract)

    video_id = info.get("id")
    expected_mp3_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")

    if not os.path.isfile(expected_mp3_path):
        raise FileNotFoundError(f"Downloaded file not found at expected path: {expected_mp3_path}")

    return Track(
        title=info.get("title", "Unknown title"),
        filepath=expected_mp3_path,
        webpage_url=info.get("webpage_url"),
        duration=info.get("duration"),
        requester=requester,
    )


def format_duration(seconds):
    if not seconds:
        return "Live/Unknown"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


async def play_next(guild_id):
    state = get_music_state(guild_id)
    async with state.loop_lock:
        if not state.queue:
            state.current = None
            return
        track = state.queue.pop(0)
        state.current = track

        if state.voice_client is None or not state.voice_client.is_connected():
            return

        ffmpeg_options = {
            "before_options": FFMPEG_OPTIONS_TEMPLATE["before_options"],
            "options": FFMPEG_OPTIONS_TEMPLATE["options"].format(volume=state.volume),
        }
        print(f"[FFMPEG] Launching with executable={FFMPEG_PATH}, file={track.filepath}")
        print(f"[FFMPEG] File exists: {os.path.isfile(track.filepath)}")
        try:
            source = discord.FFmpegPCMAudio(
                track.filepath, executable=FFMPEG_PATH, stderr=sys.stdout, **ffmpeg_options
            )
        except Exception as e:
            print(f"[FFMPEG] Failed to start FFmpeg process: {e}")
            state.current = None
            return

        def after_play(error):
            if error:
                print(f"Player error: {error}")
            # Nettoie le fichier mp3 seulement s'il n'est pas remis en queue (cas /volume qui relance le morceau courant)
            still_queued = state.queue and state.queue[0] is track
            if not still_queued:
                try:
                    if os.path.isfile(track.filepath):
                        os.remove(track.filepath)
                except Exception as cleanup_error:
                    print(f"Cleanup error: {cleanup_error}")
            fut = asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
            try:
                fut.result()
            except Exception as e:
                print(f"after_play error: {e}")

        state.voice_client.play(source, after=after_play)


async def ensure_voice_connected(interaction: discord.Interaction):
    """Connecte (ou déplace) le bot dans le salon vocal de l'utilisateur. Retourne le GuildMusicState, ou None si erreur déjà gérée."""
    if interaction.user.voice is None or interaction.user.voice.channel is None:
        embed = discord.Embed(description="❌ You need to be in a voice channel.", color=0xff0000)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return None

    state = get_music_state(interaction.guild_id)
    voice_channel = interaction.user.voice.channel

    if state.voice_client is None or not state.voice_client.is_connected():
        state.voice_client = await voice_channel.connect()
    elif state.voice_client.channel != voice_channel:
        await state.voice_client.move_to(voice_channel)

    return state


async def queue_and_play(interaction: discord.Interaction, state: "GuildMusicState", track: "Track"):
    """Ajoute un track déjà résolu à la queue et lance la lecture si rien ne joue."""
    state.queue.append(track)

    if state.voice_client.is_playing() or state.voice_client.is_paused():
        embed = discord.Embed(title="➕ Added to queue", color=0x3399ff)
        embed.add_field(name="Title", value=track.title, inline=False)
        embed.add_field(name="Duration", value=format_duration(track.duration), inline=True)
        embed.add_field(name="Position", value=f"{len(state.queue)}", inline=True)
        await interaction.followup.send(embed=embed)
    else:
        embed = discord.Embed(title="🎵 Now Playing", color=0x00cc00)
        embed.add_field(name="Title", value=track.title, inline=False)
        embed.add_field(name="Duration", value=format_duration(track.duration), inline=True)
        embed.add_field(name="Requested by", value=track.requester.mention, inline=True)
        await interaction.followup.send(embed=embed)
        await play_next(interaction.guild_id)


_autocomplete_cache = {}  # {query_lowercase: (timestamp, [entries])}
AUTOCOMPLETE_CACHE_TTL = 300  # 5 minutes


async def play_autocomplete(interaction: discord.Interaction, current: str):
    """
    Callback d'autocomplete pour /play : propose des titres en live pendant la frappe.
    Discord impose ~3s de timeout et spamme une requête par frappe, donc on cache les résultats
    et on attend un minimum de caractères avant de chercher, comme FlaviBot.
    """
    current = current.strip()
    if len(current) < 2 or current.startswith("http"):
        return []

    cache_key = current.lower()
    cached = _autocomplete_cache.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < AUTOCOMPLETE_CACHE_TTL:
        entries = cached[1]
    else:
        try:
            # Discord coupe la connexion après ~3s ; on se laisse une marge pour répondre à temps
            entries = await asyncio.wait_for(search_youtube(current, max_results=5), timeout=2.5)
        except asyncio.TimeoutError:
            print(f"autocomplete search timeout for query: {current}")
            return []
        except Exception as e:
            print(f"autocomplete search error: {e}")
            return []
        _autocomplete_cache[cache_key] = (now, entries)

    choices = []
    for entry in entries[:8]:
        title = entry.get("title", "Unknown")
        duration = format_duration(entry.get("duration"))
        label = f"{title} · {duration}"[:100]
        video_id = entry.get("id")
        raw_url = entry.get("url")
        if video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        elif raw_url and raw_url.startswith("http"):
            video_url = raw_url
        else:
            # dernier recours : certains extracteurs renvoient l'id brut dans "url"
            video_url = f"https://www.youtube.com/watch?v={raw_url}" if raw_url else None
        if not video_url:
            continue
        choices.append(app_commands.Choice(name=label, value=video_url[:100]))
    return choices


@bot.tree.command(name="play", description="Play a song from YouTube, Spotify or SoundCloud")
@app_commands.autocomplete(query=play_autocomplete)
async def play(interaction: discord.Interaction, query: str):
    if not await check_access(interaction, "play", None):
        return

    await interaction.response.defer()
    state = await ensure_voice_connected(interaction)
    if state is None:
        return

    try:
        track = await resolve_query(query, interaction.user)
    except Exception as e:
        error_detail = str(e)[:200]
        embed = discord.Embed(
            description=f"❌ Couldn't find or load that track.\n```{error_detail}```",
            color=0xff0000,
        )
        await interaction.followup.send(embed=embed)
        print(f"resolve_query error: {e}")
        return

    await queue_and_play(interaction, state, track)


async def search_youtube(query, max_results=5):
    """Renvoie une liste de résultats (titre, durée, url) depuis une recherche YouTube, sans télécharger l'audio."""
    loop = asyncio.get_event_loop()
    search_opts = dict(YTDL_OPTIONS)
    search_opts["extract_flat"] = True  # juste les métadonnées, pas l'URL de stream complète (plus rapide)

    def extract():
        with yt_dlp.YoutubeDL(search_opts) as ytdl_search:
            info = ytdl_search.extract_info(f"ytsearch{max_results}:{query}", download=False)
            return info.get("entries", [])

    return await loop.run_in_executor(None, extract)


class SearchResultSelect(discord.ui.Select):
    def __init__(self, results, requester):
        self.results = results
        self.requester = requester
        options = []
        for i, entry in enumerate(results):
            title = entry.get("title", "Unknown")[:90]
            duration = format_duration(entry.get("duration"))
            options.append(discord.SelectOption(label=f"{i + 1}. {title}", description=duration, value=str(i)))
        super().__init__(placeholder="Choose a track to play...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        chosen = self.results[int(self.values[0])]
        video_id = chosen.get("id")
        raw_url = chosen.get("url")
        if video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        elif raw_url and raw_url.startswith("http"):
            video_url = raw_url
        else:
            video_url = f"https://www.youtube.com/watch?v={raw_url}" if raw_url else None

        if not video_url:
            embed = discord.Embed(description="❌ Couldn't load that track.", color=0xff0000)
            await interaction.followup.send(embed=embed)
            return

        state = await ensure_voice_connected(interaction)
        if state is None:
            return

        try:
            track = await resolve_query(video_url, self.requester)
        except Exception as e:
            error_detail = str(e)[:200]
            embed = discord.Embed(
                description=f"❌ Couldn't load that track.\n```{error_detail}```",
                color=0xff0000,
            )
            await interaction.followup.send(embed=embed)
            print(f"resolve_query error (search select): {e}")
            return

        await queue_and_play(interaction, state, track)

        # Désactive le menu une fois un choix fait, pour éviter les doubles lectures
        self.disabled = True
        try:
            await interaction.message.edit(view=self.view)
        except Exception:
            pass


class SearchResultView(discord.ui.View):
    def __init__(self, results, requester):
        super().__init__(timeout=60)
        self.add_item(SearchResultSelect(results, requester))


@bot.tree.command(name="search", description="Search for a song and choose from a list of results")
async def search(interaction: discord.Interaction, query: str):
    if not await check_access(interaction, "search", None):
        return

    await interaction.response.defer()

    try:
        results = await search_youtube(query, max_results=5)
    except Exception as e:
        embed = discord.Embed(description="❌ Search failed, try again.", color=0xff0000)
        await interaction.followup.send(embed=embed)
        print(f"search_youtube error: {e}")
        return

    if not results:
        embed = discord.Embed(description="❌ No results found.", color=0xff0000)
        await interaction.followup.send(embed=embed)
        return

    embed = discord.Embed(title=f"🔎 Search results for \"{query}\"", color=0x3399ff)
    lines = []
    for i, entry in enumerate(results):
        title = entry.get("title", "Unknown")
        duration = format_duration(entry.get("duration"))
        lines.append(f"**{i + 1}.** {title} · {duration}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Select a track from the menu below")

    view = SearchResultView(results, interaction.user)
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    if not await check_access(interaction, "pause", None):
        return
    state = get_music_state(interaction.guild_id)
    if state.voice_client and state.voice_client.is_playing():
        state.voice_client.pause()
        embed = discord.Embed(description="⏸️ Paused.", color=0xff6600)
        await interaction.response.send_message(embed=embed)
    else:
        embed = discord.Embed(description="❌ Nothing is playing.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="resume", description="Resume the paused song")
async def resume(interaction: discord.Interaction):
    if not await check_access(interaction, "resume", None):
        return
    state = get_music_state(interaction.guild_id)
    if state.voice_client and state.voice_client.is_paused():
        state.voice_client.resume()
        embed = discord.Embed(description="▶️ Resumed.", color=0x00cc00)
        await interaction.response.send_message(embed=embed)
    else:
        embed = discord.Embed(description="❌ Nothing is paused.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    if not await check_access(interaction, "skip", None):
        return
    state = get_music_state(interaction.guild_id)
    if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
        embed = discord.Embed(description="⏭️ Skipped.", color=0x3399ff)
        await interaction.response.send_message(embed=embed)
        state.voice_client.stop()  # déclenche after_play -> play_next automatiquement
    else:
        embed = discord.Embed(description="❌ Nothing is playing.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="volume", description="Set the playback volume (0 to 200%)")
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 200]):
    if not await check_access(interaction, "volume", None):
        return
    state = get_music_state(interaction.guild_id)
    state.volume = percent / 100.0

    if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
        # Redémarre la source avec le nouveau volume sans perdre la position de la queue.
        # FFmpeg ne permet pas de changer le volume "à chaud" sans relancer le flux ; comme on
        # ne peut pas reprendre exactement où on en était facilement, on relance le morceau courant.
        current_track = state.current
        if current_track:
            state.queue.insert(0, current_track)
        state.voice_client.stop()

    embed = discord.Embed(description=f"🔊 Volume set to **{percent}%**.", color=0x3399ff)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction):
    if not await check_access(interaction, "stop", None):
        return
    state = get_music_state(interaction.guild_id)
    # Nettoie les fichiers mp3 des morceaux encore en attente (espace disque limité sur Render)
    for queued_track in state.queue:
        try:
            if os.path.isfile(queued_track.filepath):
                os.remove(queued_track.filepath)
        except Exception as e:
            print(f"Cleanup error on stop: {e}")
    state.queue.clear()
    state.current = None
    if state.voice_client:
        if state.voice_client.is_playing() or state.voice_client.is_paused():
            state.voice_client.stop()
        await state.voice_client.disconnect()
        state.voice_client = None
    embed = discord.Embed(description="⏹️ Stopped and cleared the queue.", color=0xff0000)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="queue", description="Show the current music queue")
async def queue_cmd(interaction: discord.Interaction):
    state = get_music_state(interaction.guild_id)
    embed = discord.Embed(title="🎶 Music Queue", color=0x3399ff)

    if state.current:
        embed.add_field(
            name="Now Playing",
            value=f"{state.current.title} · {format_duration(state.current.duration)}",
            inline=False,
        )
    else:
        embed.add_field(name="Now Playing", value="Nothing", inline=False)

    if state.queue:
        lines = []
        for i, track in enumerate(state.queue[:10], start=1):
            lines.append(f"{i}. {track.title} · {format_duration(track.duration)}")
        embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
        if len(state.queue) > 10:
            embed.set_footer(text=f"+ {len(state.queue) - 10} more in queue")
    else:
        embed.add_field(name="Up Next", value="Queue is empty", inline=False)

    await interaction.response.send_message(embed=embed)

# ============================================================
# ===================  AUTO-MODERATION  =====================
# ============================================================

# Suivi en mémoire des messages récents par utilisateur, pour la détection de spam.
# Pas besoin de persister ça en DB : ça repart à zéro si le bot redémarre, ce qui est très bien.
_recent_messages = {}  # {(guild_id, user_id): [timestamps]}

# Suivi en mémoire des arrivées récentes par serveur, pour la détection de raid.
_recent_joins = {}  # {guild_id: [timestamps]}

SPAM_MESSAGE_COUNT = 10
SPAM_WINDOW_SECONDS = 5
CAPS_MIN_LENGTH = 10
CAPS_RATIO_THRESHOLD = 0.7
INVITE_LINK_RE = re.compile(r"(discord\.gg/|discord(?:app)?\.com/invite/)", re.IGNORECASE)
RAID_JOIN_COUNT = 5
RAID_WINDOW_SECONDS = 10


def is_automod_exempt(member, cfg):
    """Les modérateurs (rôles autorisés ou permission gérer les messages) ne sont jamais auto-modérés."""
    if member.guild_permissions.manage_messages:
        return True
    allowed = set(cfg.get("allowed_roles", []))
    member_roles = {role.id for role in member.roles}
    if member_roles & allowed:
        return True
    return False


def check_spam(guild_id, user_id):
    key = (guild_id, user_id)
    now = time.time()
    timestamps = _recent_messages.get(key, [])
    timestamps = [t for t in timestamps if now - t < SPAM_WINDOW_SECONDS]
    timestamps.append(now)
    _recent_messages[key] = timestamps
    return len(timestamps) > SPAM_MESSAGE_COUNT


def check_raid(guild_id):
    """Retourne True si RAID_JOIN_COUNT membres ont rejoint en moins de RAID_WINDOW_SECONDS."""
    now = time.time()
    timestamps = _recent_joins.get(guild_id, [])
    timestamps = [t for t in timestamps if now - t < RAID_WINDOW_SECONDS]
    timestamps.append(now)
    _recent_joins[guild_id] = timestamps
    return len(timestamps) >= RAID_JOIN_COUNT




def check_caps(content):
    letters = [c for c in content if c.isalpha()]
    if len(content) < CAPS_MIN_LENGTH or len(letters) < CAPS_MIN_LENGTH:
        return False
    upper_count = sum(1 for c in letters if c.isupper())
    return (upper_count / len(letters)) > CAPS_RATIO_THRESHOLD


def check_invite_link(content):
    return bool(INVITE_LINK_RE.search(content))


async def apply_automod_action(message, violation_type, reason):
    """Supprime le message, ajoute un avertissement, logue la sanction et notifie dans le channel + logs."""
    try:
        await message.delete()
    except Exception as e:
        print(f"AutoMod: failed to delete message: {e}")

    count = get_warns(message.guild.id, message.author.id) + 1
    set_warns(message.guild.id, message.author.id, count)
    log_sanction(message.guild.id, message.author.id, violation_type, reason, "automod")

    warning_embed = discord.Embed(
        description=f"⚠️ {message.author.mention}, {reason}. This has been logged as a warning ({count} total).",
        color=0xffcc00,
    )
    try:
        warning_msg = await message.channel.send(embed=warning_embed)
        await asyncio.sleep(5)
        await warning_msg.delete()
    except Exception as e:
        print(f"AutoMod: failed to send/delete warning message: {e}")

    cfg = get_config(message.guild.id)
    log_channel = discord.utils.get(message.guild.text_channels, name=cfg.get("logs_channel", "logs"))
    if log_channel:
        icon = SANCTION_ICONS.get(violation_type, "🤖")
        log_embed = discord.Embed(title=f"{icon} AutoMod Action", color=0xffcc00)
        log_embed.add_field(name="User", value=f"**{message.author}**", inline=True)
        log_embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        log_embed.add_field(name="Reason", value=reason, inline=False)
        log_embed.add_field(name="Total Warnings", value=f"{count}", inline=True)
        log_embed.set_thumbnail(url=message.author.display_avatar.url)
        await log_channel.send(embed=log_embed)


@bot.event
async def on_message(message):
    if message.guild is None or message.author.bot:
        return

    cfg = get_config(message.guild.id)

    if not is_automod_exempt(message.author, cfg):
        if check_invite_link(message.content):
            await apply_automod_action(message, "automod_link", "posting an invite link")
            return
        if check_spam(message.guild.id, message.author.id):
            await apply_automod_action(message, "automod_spam", "sending messages too quickly")
            return
        if check_caps(message.content):
            await apply_automod_action(message, "automod_caps", "excessive use of capital letters")
            return

    # Nécessaire pour que les éventuelles commandes à préfixe continuent de fonctionner
    # (aucune n'est définie actuellement, mais ça évite un piège classique si on en ajoute plus tard)
    await bot.process_commands(message)

# Logs
@bot.event
async def on_member_join(member):
    cfg = get_config(member.guild.id)
    autorole_id = cfg.get("autorole")
    # On ne file pas l'autorole si le serveur est déjà verrouillé (raid en cours) :
    # pas de vérification anti-alt/anti-raid en amont, donc mieux vaut ne rien
    # distribuer automatiquement tant que la situation n'est pas confirmée saine.
    if autorole_id and member.guild.id not in bot.locked_guilds:
        role = member.guild.get_role(autorole_id)
        if role:
            await member.add_roles(role)
    channel = discord.utils.get(member.guild.text_channels, name=cfg.get("logs_channel", "logs"))
    if channel:
        embed = discord.Embed(title="✅ Member Joined", color=0x00cc00)
        embed.add_field(name="User", value=f"**{member}**", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)

    # Anti-raid : si RAID_JOIN_COUNT membres rejoignent en moins de RAID_WINDOW_SECONDS, on lock le serveur.
    if check_raid(member.guild.id) and member.guild.id not in bot.locked_guilds:
        bot.locked_guilds.add(member.guild.id)
        alert = discord.Embed(
            title="🚨 Raid Detected",
            description=(
                f"{RAID_JOIN_COUNT}+ members joined **{member.guild.name}** in under {RAID_WINDOW_SECONDS} seconds.\n"
                "The server has been **automatically locked** — most commands now require Administrator until it's unlocked.\n\n"
                f"Use `/botunlock` (server owner only) or the mobile app to unlock once it's safe."
            ),
            color=0xff0000,
        )
        if channel:
            await channel.send(embed=alert)
        try:
            owner = member.guild.owner or await member.guild.fetch_owner()
            await owner.send(embed=alert)
        except Exception as e:
            print(f"Anti-raid owner DM failed: {e}")

@bot.event
async def on_member_remove(member):
    cfg = get_config(member.guild.id)
    channel = discord.utils.get(member.guild.text_channels, name=cfg.get("logs_channel", "logs"))
    if channel:
        embed = discord.Embed(title="❌ Member Left", color=0xff0000)
        embed.add_field(name="User", value=f"**{member}**", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)

@bot.event
async def on_message_delete(message):
    if message.guild is None or message.author.bot:
        return
    cfg = get_config(message.guild.id)
    channel = discord.utils.get(message.guild.text_channels, name=cfg.get("logs_channel", "logs"))
    if channel:
        embed = discord.Embed(title="🗑️ Message Deleted", color=0xff6600)
        embed.add_field(name="Author", value=f"**{message.author}**", inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Content", value=message.content or "*(empty)*", inline=False)
        await channel.send(embed=embed)

@bot.event
async def on_message_edit(before, after):
    if before.guild is None or before.author.bot:
        return
    cfg = get_config(before.guild.id)
    channel = discord.utils.get(before.guild.text_channels, name=cfg.get("logs_channel", "logs"))
    if channel:
        embed = discord.Embed(title="✏️ Message Edited", color=0x3399ff)
        embed.add_field(name="Author", value=f"**{before.author}**", inline=True)
        embed.add_field(name="Channel", value=before.channel.mention, inline=True)
        embed.add_field(name="Before", value=before.content or "*(empty)*", inline=False)
        embed.add_field(name="After", value=after.content or "*(empty)*", inline=False)
        await channel.send(embed=embed)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.member is None or payload.member.bot:
        return
    emoji_str = str(payload.emoji)
    doc = get_reaction_role(payload.guild_id, payload.message_id, emoji_str)
    if not doc:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    role = guild.get_role(int(doc["role_id"]))
    if role and role < guild.me.top_role:
        try:
            await payload.member.add_roles(role, reason="Reaction role")
        except Exception as e:
            print(f"Reaction role add error: {e}")


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    emoji_str = str(payload.emoji)
    doc = get_reaction_role(payload.guild_id, payload.message_id, emoji_str)
    if not doc:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    if not member or member.bot:
        return
    role = guild.get_role(int(doc["role_id"]))
    if role and role < guild.me.top_role:
        try:
            await member.remove_roles(role, reason="Reaction role removed")
        except Exception as e:
            print(f"Reaction role remove error: {e}")


# ============================================================
# ===================  API REST (App Android) =================
# ============================================================

api = Flask('')

def require_api_key(f):
    """Décorateur : vérifie le header X-API-Key sur chaque requête protégée.
    Deux clés sont acceptées :
    - la clé "maîtresse" définie dans la variable d'env API_KEY sur Render (accès à tous les serveurs)
    - la clé propre à UN serveur, générée via /config apikey (accès à ce serveur uniquement)

    Pose g.auth_guild_id = None si la clé maîtresse est utilisée (accès total),
    ou l'ID du serveur si une clé propre à un serveur est utilisée (accès limité à ce serveur).
    Utile pour les routes sans guild_id dans l'URL (/api/health, /api/guilds).
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-API-Key")
        if not key:
            return jsonify({"error": "Missing X-API-Key header"}), 401
        if API_KEY and key == API_KEY:
            g.auth_guild_id = None
            return f(*args, **kwargs)
        guild_id = kwargs.get("guild_id")
        if guild_id:
            cfg = get_config(guild_id)
            if cfg.get("api_key") and key == cfg["api_key"]:
                g.auth_guild_id = str(guild_id)
                return f(*args, **kwargs)
        elif bot.is_ready():
            # Pas de guild_id dans l'URL (ex: /api/health, /api/guilds) : on cherche
            # à quel serveur cette clé appartient parmi tous les serveurs du bot.
            for candidate_guild in bot.guilds:
                cfg = get_config(candidate_guild.id)
                if cfg.get("api_key") and key == cfg["api_key"]:
                    g.auth_guild_id = str(candidate_guild.id)
                    return f(*args, **kwargs)
        return jsonify({"error": "Unauthorized"}), 401
    return wrapper

def run_coroutine(coro, timeout=10):
    """
    Exécute une coroutine discord.py depuis un thread Flask (sync),
    en la poussant dans l'event loop du bot, et attend le résultat.
    """
    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
    return future.result(timeout=timeout)

async def _send_log_embed(guild, embed):
    """Envoie un embed dans le channel de logs configuré pour ce serveur."""
    cfg = get_config(guild.id)
    channel = discord.utils.get(guild.text_channels, name=cfg.get("logs_channel", "logs"))
    if channel:
        await channel.send(embed=embed)

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

@api.route('/')
def home():
    return "Bot is alive!"

@api.route('/api/health', methods=['GET'])
@require_api_key
def health():
    if g.auth_guild_id:
        # Clé propre à un serveur : "locked" reflète uniquement ce serveur, pas les autres.
        return jsonify({
            "online": bot.is_ready(),
            "locked": int(g.auth_guild_id) in bot.locked_guilds,
            "guild_count": 1
        })
    return jsonify({
        "online": bot.is_ready(),
        "locked": len(bot.locked_guilds) > 0,
        "guild_count": len(bot.guilds) if bot.is_ready() else 0
    })

@api.route('/api/guilds', methods=['GET'])
@require_api_key
def list_guilds():
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    if g.auth_guild_id:
        # Clé propre à un serveur : ne renvoie que CE serveur, jamais les autres.
        guild = bot.get_guild(int(g.auth_guild_id))
        if not guild:
            return jsonify([])
        return jsonify([{"id": str(guild.id), "name": guild.name, "member_count": guild.member_count}])
    guilds = [{"id": str(gd.id), "name": gd.name, "member_count": gd.member_count} for gd in bot.guilds]
    return jsonify(guilds)

@api.route('/api/guilds/<guild_id>/stats', methods=['GET'])
@require_api_key
def guild_stats(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    async def _stats():
        bans = [b async for b in guild.bans()]
        muted = [m for m in guild.members if m.is_timed_out()]
        return len(bans), len(muted)

    try:
        ban_count, muted_count = run_coroutine(_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    warned_count = warns_col.count_documents({"guild_id": str(guild_id), "count": {"$gt": 0}})

    return jsonify({
        "guild_id": str(guild.id),
        "name": guild.name,
        "member_count": guild.member_count,
        "ban_count": ban_count,
        "muted_count": muted_count,
        "warned_count": warned_count,
        "locked": int(guild_id) in bot.locked_guilds
    })

@api.route('/api/guilds/<guild_id>/config', methods=['GET'])
@require_api_key
def get_guild_config(guild_id):
    cfg = get_config(guild_id)
    cfg.pop("_id", None)
    cfg.pop("api_key", None)  # jamais renvoyée en clair
    return jsonify(cfg)

@api.route('/api/guilds/<guild_id>/config', methods=['POST'])
@require_api_key
def update_guild_config(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    allowed_keys = {"logs_channel", "autorole", "allowed_roles", "command_roles"}
    updated = {}
    for key, value in data.items():
        if key in allowed_keys:
            update_config(guild_id, key, value)
            updated[key] = value

    if not updated:
        return jsonify({"error": "No valid fields provided"}), 400
    return jsonify({"success": True, "updated": updated})

@api.route('/api/guilds/<guild_id>/lock', methods=['POST'])
@require_api_key
def lock_bot_route(guild_id):
    bot.locked_guilds.add(int(guild_id))
    return jsonify({"success": True, "locked": True})

@api.route('/api/guilds/<guild_id>/unlock', methods=['POST'])
@require_api_key
def unlock_bot_route(guild_id):
    bot.locked_guilds.discard(int(guild_id))
    return jsonify({"success": True, "locked": False})

@api.route('/api/guilds/<guild_id>/broadcast', methods=['POST'])
@require_api_key
def broadcast_route(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    message = data.get("message")
    channel_id = data.get("channel_id")
    mention = data.get("mention", "none")

    if not message or not channel_id:
        return jsonify({"error": "message and channel_id are required"}), 400

    channel = guild.get_channel(int(channel_id))
    if not channel:
        return jsonify({"error": "Channel not found"}), 404

    if mention == "everyone":
        ping = "@everyone"
    elif mention == "here":
        ping = "@here"
    else:
        role = discord.utils.get(guild.roles, name=mention)
        ping = role.mention if role else None

    embed = discord.Embed(description=message, color=0x3399ff)
    embed.set_footer(text="📢 Sent from the mobile app")

    async def _send():
        await channel.send(content=ping, embed=embed)

    try:
        run_coroutine(_send())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"success": True})

@api.route('/api/guilds/<guild_id>/members/<user_id>/ban', methods=['POST'])
@require_api_key
def ban_member_route(guild_id, user_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "Banned via mobile app")

    async def _ban():
        member = guild.get_member(int(user_id))
        if not member:
            return False, "Member not found"
        if member.top_role >= guild.me.top_role:
            return False, "Role too high"
        await member.ban(reason=reason)
        log_sanction(guild_id, user_id, "ban", reason, "mobile_app")
        await log_ban(guild, member, reason)
        return True, None

    try:
        success, error = run_coroutine(_ban())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})

@api.route('/api/guilds/<guild_id>/members/<user_id>/kick', methods=['POST'])
@require_api_key
def kick_member_route(guild_id, user_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "Kicked via mobile app")

    async def _kick():
        member = guild.get_member(int(user_id))
        if not member:
            return False, "Member not found"
        if member.top_role >= guild.me.top_role:
            return False, "Role too high"
        await member.kick(reason=reason)
        log_sanction(guild_id, user_id, "kick", reason, "mobile_app")
        await log_kick(guild, member, reason)
        return True, None

    try:
        success, error = run_coroutine(_kick())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})

@api.route('/api/guilds/<guild_id>/members/<user_id>/warnings', methods=['GET'])
@require_api_key
def get_warnings_route(guild_id, user_id):
    count = get_warns(guild_id, user_id)
    return jsonify({"user_id": user_id, "warnings": count})

@api.route('/api/guilds/<guild_id>/warnlist', methods=['GET'])
@require_api_key
def warnlist_route(guild_id):
    docs = list(warns_col.find({"guild_id": str(guild_id), "count": {"$gt": 0}}))
    result = [{"user_id": d["user_id"], "count": d["count"]} for d in docs]
    return jsonify(result)

@api.route('/api/guilds/<guild_id>/channels', methods=['GET'])
@require_api_key
def list_channels_route(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404
    channels = [{"id": str(c.id), "name": c.name} for c in guild.text_channels]
    return jsonify(channels)

@api.route('/api/guilds/<guild_id>/roles', methods=['GET'])
@require_api_key
def list_roles_route(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404
    roles = [{"id": str(r.id), "name": r.name} for r in guild.roles if not r.is_default()]
    return jsonify(roles)

@api.route('/api/guilds/<guild_id>/members', methods=['GET'])
@require_api_key
def list_members_route(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404
    members = [
        {
            "id": str(m.id),
            "name": m.display_name,
            "username": str(m),
            "avatar_url": m.display_avatar.url,
            "is_muted": m.is_timed_out()
        }
        for m in guild.members if not m.bot
    ]
    return jsonify(members)

@api.route('/api/guilds/<guild_id>/members/<user_id>/mute', methods=['POST'])
@require_api_key
def mute_member_route(guild_id, user_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    minutes = int(data.get("minutes", 10))
    reason = data.get("reason") or "Muted by mobile app"

    async def _mute():
        member = guild.get_member(int(user_id))
        if not member:
            return False, "Member not found"
        if member.top_role >= guild.me.top_role:
            return False, "Role too high"
        duration = datetime.timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
        log_sanction(guild_id, user_id, "mute", f"{reason} ({minutes} min)", "mobile_app")
        await log_mute(guild, member, minutes, reason)
        return True, None

    try:
        success, error = run_coroutine(_mute())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})

@api.route('/api/guilds/<guild_id>/members/<user_id>/unmute', methods=['POST'])
@require_api_key
def unmute_member_route(guild_id, user_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    async def _unmute():
        member = guild.get_member(int(user_id))
        if not member:
            return False, "Member not found"
        await member.timeout(None)
        await log_unmute(guild, member)
        return True, None

    try:
        success, error = run_coroutine(_unmute())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})

@api.route('/api/guilds/<guild_id>/members/<user_id>/warn', methods=['POST'])
@require_api_key
def warn_member_route(guild_id, user_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    reason = data.get("reason") or "Warned by mobile app"

    member = guild.get_member(int(user_id))
    if not member:
        return jsonify({"error": "Member not found"}), 404

    count = get_warns(guild_id, user_id) + 1
    set_warns(guild_id, user_id, count)
    log_sanction(guild_id, user_id, "warn", reason, "mobile_app")

    try:
        run_coroutine(log_warn(guild, member, reason, count))
    except Exception:
        pass  # le warn est déjà enregistré, le log est secondaire

    return jsonify({"success": True, "warnings": count, "reason": reason})

@api.route('/api/guilds/<guild_id>/members/<user_id>/unwarn', methods=['POST'])
@require_api_key
def unwarn_member_route(guild_id, user_id):
    count = get_warns(guild_id, user_id)
    if count == 0:
        return jsonify({"error": "No warnings to remove"}), 400
    count -= 1
    set_warns(guild_id, user_id, count)

    if bot.is_ready():
        guild = bot.get_guild(int(guild_id))
        if guild:
            member = guild.get_member(int(user_id))
            if member:
                try:
                    run_coroutine(log_unwarn(guild, member, count))
                except Exception:
                    pass

    return jsonify({"success": True, "warnings": count})


# ============================================================
# ============ DASHBOARD WEB (OAuth2 Discord) ================
# ============================================================
# Dashboard admin-only : login via Discord, accès limité aux serveurs
# ou l'utilisateur a la permission Administrator ET ou le bot est présent.

api.secret_key = os.environ["FLASK_SECRET_KEY"]
api.config.update(
    SESSION_COOKIE_SECURE=True,     # cookie envoyé que en HTTPS
    SESSION_COOKIE_HTTPONLY=True,   # inaccessible en JS (anti XSS)
    SESSION_COOKIE_SAMESITE="Lax",  # anti CSRF de base
)

DASH_CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
DASH_CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
DASH_REDIRECT_URI = os.environ["DISCORD_REDIRECT_URI"]

DISCORD_API_BASE = "https://discord.com/api"
ADMINISTRATOR_PERM = 0x8
GUILD_ID_RE = re.compile(r"^\d{17,19}$")  # valide un vrai snowflake Discord


def dash_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "access_token" not in session:
            return redirect(url_for("dash_login"))
        return f(*args, **kwargs)
    return wrapper


def get_user_admin_guilds():
    """Revient TOUJOURS chercher la vérité chez Discord, jamais en cache client."""
    r = requests.get(
        f"{DISCORD_API_BASE}/users/@me/guilds",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        timeout=10,
    )
    if r.status_code == 401:
        session.clear()
        return None
    guilds = r.json()
    return [
        g_ for g_ in guilds
        if int(g_["permissions"]) & ADMINISTRATOR_PERM == ADMINISTRATOR_PERM
    ]


def dash_guild_admin_required(f):
    """Vérifie que guild_id dans l'URL est un serveur où l'user est admin ET où le bot est présent."""
    @wraps(f)
    def wrapper(guild_id, *args, **kwargs):
        if not GUILD_ID_RE.match(guild_id):
            return jsonify({"error": "Invalid guild id"}), 400
        admin_guilds = get_user_admin_guilds()
        if admin_guilds is None:
            return redirect(url_for("dash_login"))
        admin_guild_ids = {g_["id"] for g_ in admin_guilds}
        if guild_id not in admin_guild_ids:
            return jsonify({"error": "Forbidden"}), 403
        if not bot.is_ready() or not bot.get_guild(int(guild_id)):
            return jsonify({"error": "Bot not present on this server"}), 403
        return f(guild_id, *args, **kwargs)
    return wrapper


@api.route("/login")
def dash_login():
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    params = {
        "client_id": DASH_CLIENT_ID,
        "redirect_uri": DASH_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return redirect(f"{DISCORD_API_BASE}/oauth2/authorize?{query}")


@api.route("/callback")
def dash_callback():
    state = request.args.get("state")
    if not state or state != session.pop("oauth_state", None):
        return jsonify({"error": "Invalid state"}), 403

    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Missing code"}), 400

    token_r = requests.post(
        f"{DISCORD_API_BASE}/oauth2/token",
        data={
            "client_id": DASH_CLIENT_ID,
            "client_secret": DASH_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DASH_REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    if token_r.status_code != 200:
        return jsonify({"error": "Token exchange failed"}), 400

    token_data = token_r.json()
    session["access_token"] = token_data["access_token"]
    session["refresh_token"] = token_data["refresh_token"]

    user_r = requests.get(
        f"{DISCORD_API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {token_data['access_token']}"},
        timeout=10,
    )
    session["dash_user"] = user_r.json()

    return redirect(url_for("dash_home"))


@api.route("/logout")
def dash_logout():
    session.clear()
    return redirect(url_for("dash_login"))


# ---------- Templates HTML ----------

MODERATION_COMMANDS = [
    "ban", "kick", "mute", "unmute", "unban", "warn", "unwarn", "clear",
    "roleadd", "roleremove", "lock", "unlock", "vlock", "vunlock",
    "slowmode", "nickname", "softban", "purgeuser", "tempban", "broadcast",
]

BASE_STYLE = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

  :root {
    --ink: #0b0c12;
    --surface: #171a24;
    --surface-2: #1e2230;
    --surface-3: #262b3d;
    --line: #2a2f42;
    --text: #eceef5;
    --muted: #8b90a8;
    --raspberry: #ff5f8f;
    --raspberry-dim: #3a2230;
    --lime: #c3f24a;
    --lime-dim: #26301a;
    --amber: #ffb84d;
    --amber-dim: #3a2c14;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background:
      radial-gradient(900px 500px at 15% -10%, rgba(255,95,143,.08), transparent 60%),
      radial-gradient(700px 500px at 100% 0%, rgba(195,242,74,.06), transparent 55%),
      var(--ink);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
  }
  a { color: inherit; text-decoration: none; }
  h1, h2, h3 { font-family: 'Fraunces', serif; font-weight: 600; margin: 0; }
  code, .mono { font-family: 'JetBrains Mono', monospace; }

  @keyframes fadeUp { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
  @keyframes jiggle {
    0%, 100% { transform: translateY(-3px) rotate(-0.3deg); }
    50% { transform: translateY(-5px) rotate(0.4deg); }
  }
  @keyframes popIn { from { opacity: 0; transform: scale(.94); } to { opacity: 1; transform: scale(1); } }
  @keyframes pulseDot { 0%, 100% { box-shadow: 0 0 0 0 rgba(255,95,143,.5); } 50% { box-shadow: 0 0 0 6px rgba(255,95,143,0); } }
  @keyframes shimmer { 0% { background-position: -200px 0; } 100% { background-position: 200px 0; } }

  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 20px 32px; border-bottom: 1px solid var(--line);
    position: sticky; top: 0; background: rgba(11,12,18,.85); backdrop-filter: blur(10px);
    z-index: 10; animation: fadeUp .4s ease both;
  }
  .brand { display: flex; align-items: center; gap: 10px; font-family: 'Fraunces', serif; font-size: 20px; font-weight: 700; }
  .brand .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--raspberry); animation: pulseDot 2.4s infinite; }
  .user-chip { display: flex; align-items: center; gap: 10px; font-size: 14px; color: var(--muted); }
  .user-chip img { width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--line); }
  .logout { color: var(--muted); font-size: 13px; border: 1px solid var(--line); padding: 6px 12px; border-radius: 8px; transition: all .15s; }
  .logout:hover { color: var(--text); border-color: var(--raspberry); }

  .wrap { max-width: 900px; margin: 0 auto; padding: 40px 24px 80px; }
  .eyebrow { color: var(--raspberry); font-size: 12px; letter-spacing: .12em; text-transform: uppercase; margin-bottom: 8px; font-weight: 600; animation: fadeUp .4s ease .05s both; }
  h1 { animation: fadeUp .45s ease .08s both; }
  .lead { color: var(--muted); font-size: 15px; margin-top: 10px; max-width: 56ch; animation: fadeUp .45s ease .12s both; }

  .flash { background: var(--lime-dim); border: 1px solid var(--lime); color: var(--lime); padding: 10px 16px; border-radius: 12px 4px 12px 4px; font-size: 14px; margin: 20px 0; animation: popIn .25s ease both; }
  .flash.error { background: var(--raspberry-dim); border-color: var(--raspberry); color: var(--raspberry); }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; margin-top: 28px; }
  .card {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 26px 8px 26px 8px;
    padding: 22px; transition: transform .18s ease, border-color .18s, box-shadow .18s;
    animation: fadeUp .4s ease both;
  }
  a.card:hover { animation: jiggle .5s ease; border-color: var(--raspberry); box-shadow: 0 10px 30px -12px rgba(255,95,143,.35); }
  .card-icon {
    width: 44px; height: 44px; border-radius: 14px 4px 14px 4px;
    background: var(--surface-2); display: flex; align-items: center; justify-content: center;
    font-family: 'Fraunces', serif; font-weight: 700; font-size: 18px; color: var(--raspberry);
    margin-bottom: 14px; transition: transform .2s;
  }
  a.card:hover .card-icon { transform: rotate(-6deg) scale(1.05); }
  .card h3 { font-size: 17px; }
  .card .sub { color: var(--muted); font-size: 12px; margin-top: 6px; }
  .owner-tag { display: inline-block; margin-top: 12px; font-size: 11px; color: var(--lime); background: var(--lime-dim); padding: 3px 9px; border-radius: 999px; }

  .empty { color: var(--muted); font-size: 14px; margin-top: 28px; padding: 24px; border: 1px dashed var(--line); border-radius: 16px; animation: fadeUp .4s ease both; }

  .status-strip { display: flex; gap: 10px; margin-top: 22px; flex-wrap: wrap; animation: fadeUp .4s ease .1s both; }
  .pill { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--muted); background: var(--surface); border: 1px solid var(--line); padding: 7px 13px; border-radius: 999px; }
  .pill .pip { width: 7px; height: 7px; border-radius: 50%; background: var(--lime); }
  .pill.locked .pip { background: var(--raspberry); }

  .panel {
    background: var(--surface); border: 1px solid var(--line); border-radius: 20px 6px 20px 6px;
    padding: 26px; margin-top: 20px; animation: fadeUp .4s ease both;
  }
  .panel.danger { border-color: rgba(255,95,143,.35); }
  .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 4px; }
  .panel h2 { font-size: 16px; }
  .panel .desc { color: var(--muted); font-size: 13px; margin: 6px 0 18px; }
  .badge { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; padding: 3px 8px; border-radius: 999px; font-weight: 600; }
  .badge.owner { color: var(--amber); background: var(--amber-dim); }
  .badge.admin { color: var(--muted); background: var(--surface-3); }

  label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
  select, input[type=text] {
    width: 100%; background: var(--surface-2); border: 1px solid var(--line); color: var(--text);
    padding: 10px 12px; border-radius: 10px; font-family: 'Inter', sans-serif; font-size: 14px;
    margin-bottom: 18px; transition: border-color .15s, box-shadow .15s;
  }
  select[multiple] { min-height: 110px; }
  select:focus, input:focus { outline: none; border-color: var(--raspberry); box-shadow: 0 0 0 3px rgba(255,95,143,.12); }
  .hint { color: var(--muted); font-size: 11px; margin-top: -12px; margin-bottom: 18px; }
  .risky-warning { background: var(--amber-dim); border: 1px solid var(--amber); color: var(--amber); padding: 10px 14px; border-radius: 12px 4px 12px 4px; font-size: 12.5px; margin: -6px 0 18px; animation: popIn .2s ease both; }

  button, .btn {
    background: var(--raspberry); color: #1a0a10; border: none; font-weight: 600;
    padding: 11px 20px; border-radius: 12px 4px 12px 4px; font-size: 14px; cursor: pointer;
    transition: filter .15s, transform .1s; display: inline-flex; align-items: center; gap: 6px;
  }
  button:hover, .btn:hover { filter: brightness(1.1); }
  button:active, .btn:active { transform: scale(.97); }
  button.ghost { background: transparent; border: 1px solid var(--line); color: var(--text); }
  button.ghost:hover { border-color: var(--raspberry); }
  button.warn { background: var(--amber); }
  button.stop { background: var(--surface-3); color: var(--raspberry); border: 1px solid rgba(255,95,143,.4); }

  .back { color: var(--muted); font-size: 13px; display: inline-block; margin-bottom: 18px; transition: color .15s; }
  .back:hover { color: var(--text); }

  .row { display: flex; gap: 14px; flex-wrap: wrap; }
  .row > * { flex: 1; min-width: 180px; }

  .chip-list { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  .chip {
    display: flex; align-items: center; gap: 8px; background: var(--surface-2); border: 1px solid var(--line);
    padding: 6px 8px 6px 12px; border-radius: 999px; font-size: 12px; animation: popIn .2s ease both;
  }
  .chip form { margin: 0; }
  .chip button.x {
    background: var(--surface-3); color: var(--muted); border: none; width: 18px; height: 18px; border-radius: 50%;
    font-size: 11px; line-height: 1; padding: 0; display: flex; align-items: center; justify-content: center;
  }
  .chip button.x:hover { background: var(--raspberry-dim); color: var(--raspberry); }
  .no-perms { color: var(--muted); font-size: 13px; margin-bottom: 16px; }

  .key-box { display: flex; align-items: center; gap: 10px; background: var(--surface-2); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; margin-bottom: 14px; }
  .key-box code { flex: 1; color: var(--muted); letter-spacing: .04em; }
  .key-box.revealed code { color: var(--lime); filter: none; }
  .key-box code.masked { filter: blur(5px); user-select: none; }
  .reveal-btn { background: var(--surface-3); border: none; color: var(--text); font-size: 11px; padding: 6px 10px; border-radius: 8px; cursor: pointer; }

  .divider { border: none; border-top: 1px solid var(--line); margin: 20px 0; }
</style>
<script>
  function toggleKey(btn) {
    const box = btn.closest('.key-box');
    box.classList.toggle('revealed');
    const code = box.querySelector('code');
    code.classList.toggle('masked');
    btn.textContent = code.classList.contains('masked') ? 'Afficher' : 'Masquer';
  }
</script>
"""

TOPBAR = """
<div class="topbar">
  <div class="brand"><span class="dot"></span> Jello Bello</div>
  <div class="user-chip">
    {% if user and user.avatar %}
      <img src="https://cdn.discordapp.com/avatars/{{ user.id }}/{{ user.avatar }}.png" alt="">
    {% endif %}
    {{ user.global_name or user.username }}
    <a class="logout" href="{{ url_for('dash_logout') }}">Se déconnecter</a>
  </div>
</div>
"""

DASH_LIST_TEMPLATE = BASE_STYLE + TOPBAR + """
<div class="wrap">
  <div class="eyebrow">Tableau de bord</div>
  <h1>Tes serveurs</h1>
  <p class="lead">Seuls les serveurs où t'es Administrateur ET où Jello Bello est présent apparaissent ici.</p>

  {% for msg in get_flashed_messages() %}<div class="flash">{{ msg }}</div>{% endfor %}

  {% if guilds %}
  <div class="grid">
    {% for g in guilds %}
    <a class="card" style="animation-delay: {{ loop.index0 * 0.05 }}s" href="{{ url_for('dash_guild_page', guild_id=g.id) }}">
      <div class="card-icon">{{ g.name[0]|upper }}</div>
      <h3>{{ g.name }}</h3>
      <div class="sub mono">{{ g.id }}</div>
      {% if g.owner %}<span class="owner-tag">Propriétaire</span>{% endif %}
    </a>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty">Aucun serveur géré pour l'instant — ajoute Jello Bello sur un serveur où t'es admin pour le voir apparaître ici.</div>
  {% endif %}
</div>
"""

GUILD_PAGE_TEMPLATE = BASE_STYLE + TOPBAR + """
<div class="wrap">
  <a class="back" href="{{ url_for('dash_home') }}">&larr; Tous les serveurs</a>
  <div class="eyebrow">Configuration</div>
  <h1>{{ guild.name }}</h1>
  <p class="lead mono">{{ guild.id }}</p>

  <div class="status-strip">
    <span class="pill {{ 'locked' if is_locked else '' }}"><span class="pip"></span> Bot {{ 'verrouillé' if is_locked else 'actif' }}</span>
    <span class="pill"><span class="pip"></span> {{ channels|length }} salons texte</span>
    <span class="pill"><span class="pip"></span> {{ roles|length }} rôles</span>
    {% if is_owner %}<span class="pill"><span class="pip" style="background: var(--amber);"></span> Tu es propriétaire</span>{% endif %}
  </div>

  {% for msg in get_flashed_messages() %}<div class="flash">{{ msg }}</div>{% endfor %}

  <form method="POST" action="{{ url_for('dash_guild_page', guild_id=guild.id) }}">
    <div class="panel" style="animation-delay:.02s">
      <div class="panel-head"><h2>Salon de logs</h2><span class="badge admin">Admin</span></div>
      <div class="desc">Les actions de modération (bans, kicks, warns...) sont envoyées dans ce salon.</div>
      <label for="logs_channel">Salon</label>
      <select name="logs_channel" id="logs_channel">
        {% for c in channels %}
        <option value="{{ c.id }}" {% if c.name == cfg.logs_channel %}selected{% endif %}>#{{ c.name }}</option>
        {% endfor %}
      </select>

      <label for="autorole">Rôle automatique à l'arrivée</label>
      <select name="autorole" id="autorole" onchange="checkRiskyRole(this)">
        <option value="none" data-risky="0" {% if not cfg.autorole %}selected{% endif %}>Aucun</option>
        {% for r in roles %}
        <option value="{{ r.id }}" data-risky="{{ '1' if r.permissions.administrator or r.permissions.manage_guild or r.permissions.ban_members or r.permissions.kick_members or r.permissions.manage_roles else '0' }}" {% if cfg.autorole == r.id %}selected{% endif %}>{{ r.name }}</option>
        {% endfor %}
      </select>
      <div id="autorole-warning" class="risky-warning" style="display:none;">
        ⚠️ Ce rôle a des permissions sensibles (admin, gestion du serveur, ban/kick...). Il sera donné automatiquement à <strong>tout nouveau membre</strong>, sans aucune vérification anti-alt.
      </div>
      <button type="submit">Enregistrer</button>
    </div>
  </form>
  <script>
    function checkRiskyRole(select) {
      var opt = select.options[select.selectedIndex];
      var warn = document.getElementById('autorole-warning');
      warn.style.display = (opt.dataset.risky === '1') ? 'block' : 'none';
    }
    document.addEventListener('DOMContentLoaded', function() {
      checkRiskyRole(document.getElementById('autorole'));
    });
  </script>

  <form method="POST" action="{{ url_for('dash_automod', guild_id=guild.id) }}">
    <div class="panel" style="animation-delay:.06s">
      <div class="panel-head"><h2>Exemptions anti-raid / anti-spam</h2><span class="badge admin">Admin</span></div>
      <div class="desc">Ces rôles ne sont jamais auto-modérés (spam, caps, invitations). Les membres avec la permission "Gérer les messages" sont déjà exemptés.</div>
      <label for="allowed_roles">Rôles exemptés</label>
      <select name="allowed_roles" id="allowed_roles" multiple>
        {% for r in roles %}
        <option value="{{ r.id }}" {% if r.id in (cfg.allowed_roles or []) %}selected{% endif %}>{{ r.name }}</option>
        {% endfor %}
      </select>
      <div class="hint">Ctrl/Cmd + clic pour en sélectionner plusieurs.</div>
      <button type="submit">Enregistrer</button>
    </div>
  </form>

  {% if is_owner %}
  <div class="panel" style="animation-delay:.1s">
    <div class="panel-head"><h2>Permissions par commande</h2><span class="badge owner">Owner</span></div>
    <div class="desc">Autorise des rôles spécifiques à utiliser certaines commandes de modération, en plus des permissions Discord classiques.</div>

    {% if cfg.command_roles %}
    {% for cmd, role_ids in cfg.command_roles.items() %}
      {% if role_ids %}
      <label>/{{ cmd }}</label>
      <div class="chip-list">
        {% for rid in role_ids %}
        <div class="chip">
          {{ role_names.get(rid, rid) }}
          <form method="POST" action="{{ url_for('dash_permission_remove', guild_id=guild.id) }}">
            <input type="hidden" name="command" value="{{ cmd }}">
            <input type="hidden" name="role_id" value="{{ rid }}">
            <button type="submit" class="x" title="Retirer">&times;</button>
          </form>
        </div>
        {% endfor %}
      </div>
      {% endif %}
    {% endfor %}
    {% else %}
    <div class="no-perms">Aucune permission personnalisée pour l'instant.</div>
    {% endif %}

    <hr class="divider">
    <form method="POST" action="{{ url_for('dash_permission_add', guild_id=guild.id) }}">
      <div class="row">
        <div>
          <label for="command">Commande</label>
          <select name="command" id="command">
            {% for cmd in moderation_commands %}
            <option value="{{ cmd }}">/{{ cmd }}</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label for="role_id">Rôle autorisé</label>
          <select name="role_id" id="role_id">
            {% for r in roles %}
            <option value="{{ r.id }}">{{ r.name }}</option>
            {% endfor %}
          </select>
        </div>
      </div>
      <button type="submit">Ajouter la permission</button>
    </form>
  </div>

  <div class="panel" style="animation-delay:.14s">
    <div class="panel-head"><h2>Clé API mobile</h2><span class="badge owner">Owner</span></div>
    <div class="desc">Utilisée par l'app mobile pour gérer ce serveur uniquement.</div>
    {% if cfg.api_key %}
    <div class="key-box">
      <code class="masked mono">{{ cfg.api_key }}</code>
      <button type="button" class="reveal-btn" onclick="toggleKey(this)">Afficher</button>
    </div>
    {% else %}
    <div class="no-perms">Aucune clé générée pour l'instant.</div>
    {% endif %}
    <form method="POST" action="{{ url_for('dash_apikey_regen', guild_id=guild.id) }}" onsubmit="return confirm('Régénérer la clé API ? L\\'ancienne cessera de fonctionner immédiatement.');">
      <button type="submit" class="ghost">Régénérer la clé</button>
    </form>
  </div>

  <div class="panel danger" style="animation-delay:.18s">
    <div class="panel-head"><h2>Verrouillage du bot</h2><span class="badge owner">Owner</span></div>
    <div class="desc">
      {% if is_locked %}Le bot est verrouillé : seul toi peux utiliser ses commandes sur ce serveur.
      {% else %}En cas d'urgence, verrouille le bot pour bloquer toutes les commandes sauf les tiennes.{% endif %}
    </div>
    <form method="POST" action="{{ url_for('dash_toggle_lock', guild_id=guild.id) }}">
      {% if is_locked %}
      <button type="submit" class="warn">Déverrouiller le bot</button>
      {% else %}
      <button type="submit" class="stop">Verrouiller le bot</button>
      {% endif %}
    </form>
  </div>
  {% endif %}
</div>
"""


# ---------- Routes dashboard (HTML) ----------

def dash_is_owner(guild):
    user = session.get("dash_user") or {}
    return str(guild.owner_id) == str(user.get("id"))


def dash_owner_required(f):
    @wraps(f)
    def wrapper(guild_id, *args, **kwargs):
        guild = bot.get_guild(int(guild_id))
        if guild is None or not dash_is_owner(guild):
            return jsonify({"error": "Propriétaire du serveur requis"}), 403
        return f(guild_id, *args, **kwargs)
    return wrapper


@api.route("/dashboard")
@dash_login_required
def dash_home():
    admin_guilds = get_user_admin_guilds()
    if admin_guilds is None:
        return redirect(url_for("dash_login"))
    # ne montre que les serveurs où l'utilisateur est admin ET où le bot est présent
    bot_guild_ids = {str(g_.id) for g_ in bot.guilds} if bot.is_ready() else set()
    manageable = [g_ for g_ in admin_guilds if g_["id"] in bot_guild_ids]
    return render_template_string(
        DASH_LIST_TEMPLATE, guilds=manageable, user=session.get("dash_user")
    )


@api.route("/dashboard/<guild_id>", methods=["GET", "POST"])
@dash_login_required
@dash_guild_admin_required
def dash_guild_page(guild_id):
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return jsonify({"error": "Bot not present on this server"}), 403

    if request.method == "POST":
        # Toute valeur reçue est revalidée contre les objets réels de CE serveur
        # (jamais de confiance sur un id envoyé par le formulaire).
        logs_channel_id = request.form.get("logs_channel", "")
        if logs_channel_id.isdigit():
            channel = guild.get_channel(int(logs_channel_id))
            if channel is not None and channel in guild.text_channels:
                update_config(guild_id, "logs_channel", channel.name)

        autorole_id = request.form.get("autorole", "none")
        risky_perms = ("administrator", "manage_guild", "ban_members", "kick_members", "manage_roles")
        if autorole_id == "none":
            update_config(guild_id, "autorole", None)
        elif autorole_id.isdigit():
            role = guild.get_role(int(autorole_id))
            if role is not None:
                update_config(guild_id, "autorole", role.id)
                if any(getattr(role.permissions, p) for p in risky_perms):
                    flash(f"⚠️ Attention : le rôle « {role.name} » a des permissions sensibles et sera donné automatiquement à tout nouveau membre.")

        flash("Configuration mise à jour.")
        return redirect(url_for("dash_guild_page", guild_id=guild_id))

    cfg = get_config(guild_id)
    role_names = {r.id: r.name for r in guild.roles}
    return render_template_string(
        GUILD_PAGE_TEMPLATE,
        guild=guild,
        cfg=cfg,
        channels=guild.text_channels,
        roles=[r for r in guild.roles if not r.is_default()],
        role_names=role_names,
        moderation_commands=MODERATION_COMMANDS,
        is_owner=dash_is_owner(guild),
        is_locked=guild.id in bot.locked_guilds,
        user=session.get("dash_user"),
    )


@api.route("/dashboard/<guild_id>/automod", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
def dash_automod(guild_id):
    guild = bot.get_guild(int(guild_id))
    submitted = request.form.getlist("allowed_roles")
    valid_role_ids = {r.id for r in guild.roles}
    # ne garde que des ids qui correspondent à de vrais rôles de CE serveur
    clean = [int(rid) for rid in submitted if rid.isdigit() and int(rid) in valid_role_ids]
    update_config(guild_id, "allowed_roles", clean)
    flash("Exemptions anti-raid mises à jour.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@api.route("/dashboard/<guild_id>/permissions/add", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_permission_add(guild_id):
    guild = bot.get_guild(int(guild_id))
    command = request.form.get("command", "")
    role_id = request.form.get("role_id", "")
    if command in MODERATION_COMMANDS and role_id.isdigit():
        role = guild.get_role(int(role_id))
        if role is not None:
            add_command_role(guild_id, command, role.id)
            flash(f"{role.name} peut maintenant utiliser /{command}.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@api.route("/dashboard/<guild_id>/permissions/remove", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_permission_remove(guild_id):
    command = request.form.get("command", "")
    role_id = request.form.get("role_id", "")
    if command in MODERATION_COMMANDS and role_id.isdigit():
        remove_command_role(guild_id, command, int(role_id))
        flash(f"Permission retirée pour /{command}.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@api.route("/dashboard/<guild_id>/apikey/regenerate", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_apikey_regen(guild_id):
    new_key = secrets.token_hex(16)
    update_config(guild_id, "api_key", new_key)
    flash("Nouvelle clé API générée — l'ancienne ne fonctionne plus.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@api.route("/dashboard/<guild_id>/lock", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_toggle_lock(guild_id):
    gid = int(guild_id)
    if gid in bot.locked_guilds:
        bot.locked_guilds.discard(gid)
        flash("Bot déverrouillé.")
    else:
        bot.locked_guilds.add(gid)
        flash("Bot verrouillé — seul toi peux utiliser ses commandes ici.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


def run_api():
    port = int(os.getenv("PORT", 8080))
    api.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_api)
    t.daemon = True
    t.start()

keep_alive()

# Si Discord/Cloudflare renvoie un 429 au login (rate limit), on ne laisse pas
# le process planter : Render le relancerait instantanément, ce qui martèle
# encore plus l'endpoint de login et prolonge le blocage. On attend avec un
# backoff progressif à la place.
_login_attempt = 0
while True:
    try:
        bot.run(os.getenv("TOKEN"))
        break  # bot.run() ne revient normalement qu'à l'arrêt volontaire
    except discord.errors.HTTPException as e:
        if e.status == 429:
            _login_attempt += 1
            wait = min(60 * (2 ** (_login_attempt - 1)), 900)  # 60s, 120s, 240s... max 15 min
            print(f"[LOGIN] 429 rate limited par Discord/Cloudflare, retry dans {wait}s (tentative {_login_attempt})", flush=True)
            time.sleep(wait)
        else:
            raise
