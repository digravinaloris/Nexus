"""Discord server structure backup snapshots and safe restoration.

This module contains the Discord-facing backup logic extracted from main.py.
Persistence is accessed through the shared collection registry; it never
imports the application entrypoint.
"""
import datetime

import discord

from stores.server_backup_store import insert_server_backup


def _serialize_overwrites(channel_or_category):
    """Ne garde que les permissions par rôle (pas par membre) — c'est la
    structure qu'on veut restaurer, les cas spécifiques par membre sont
    trop fragiles à recréer fidèlement après coup."""
    data = []
    for target, overwrite in channel_or_category.overwrites.items():
        if isinstance(target, discord.Role):
            allow, deny = overwrite.pair()
            data.append({"role_name": target.name, "allow": allow.value, "deny": deny.value})
    return data


def _rebuild_overwrites(serialized_overwrites, role_name_to_role):
    result = {}
    for item in serialized_overwrites:
        role = role_name_to_role.get(item["role_name"])
        if role is None:
            continue
        result[role] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(item["allow"]), discord.Permissions(item["deny"])
        )
    return result


def create_server_backup(guild):
    roles_data = []
    for role in guild.roles:
        if role.is_default():
            continue
        roles_data.append({
            "name": role.name,
            "color": role.color.value,
            "permissions": role.permissions.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "position": role.position,
        })

    channels_data = []
    for category in guild.categories:
        channels_data.append({
            "type": "category",
            "name": category.name,
            "overwrites": _serialize_overwrites(category),
        })
    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            continue
        if isinstance(channel, discord.TextChannel):
            ch_type = "text"
        elif isinstance(channel, discord.VoiceChannel):
            ch_type = "voice"
        else:
            continue  # threads/forums/stage etc. pas gérés pour l'instant
        channels_data.append({
            "type": ch_type,
            "name": channel.name,
            "category": channel.category.name if channel.category else None,
            "topic": getattr(channel, "topic", None),
            "overwrites": _serialize_overwrites(channel),
        })

    backup_doc = {
        "guild_id": str(guild.id),
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "roles": roles_data,
        "channels": channels_data,
    }
    return insert_server_backup(backup_doc)





async def restore_server_backup(guild, backup):
    """Ne supprime jamais rien : ne recrée que les rôles/salons manquants
    (matchés par nom). Peut créer des doublons si relancé après un premier
    restore partiel qui aurait changé un nom entre-temps — c'est un
    compromis assumé pour rester simple et sans danger de suppression."""
    role_name_to_role = {r.name: r for r in guild.roles}
    created_roles = 0
    for role_data in sorted(backup["roles"], key=lambda r: r["position"]):
        if role_data["name"] in role_name_to_role:
            continue
        try:
            new_role = await guild.create_role(
                name=role_data["name"],
                color=discord.Color(role_data["color"]),
                permissions=discord.Permissions(role_data["permissions"]),
                hoist=role_data["hoist"],
                mentionable=role_data["mentionable"],
                reason="Server backup restore",
            )
            role_name_to_role[new_role.name] = new_role
            created_roles += 1
        except discord.HTTPException:
            pass

    category_name_to_category = {c.name: c for c in guild.categories}
    created_channels = 0
    for ch_data in backup["channels"]:
        if ch_data["type"] != "category" or ch_data["name"] in category_name_to_category:
            continue
        overwrites = _rebuild_overwrites(ch_data["overwrites"], role_name_to_role)
        try:
            new_cat = await guild.create_category(ch_data["name"], overwrites=overwrites, reason="Server backup restore")
            category_name_to_category[new_cat.name] = new_cat
            created_channels += 1
        except discord.HTTPException:
            pass

    existing_channel_names = {c.name for c in guild.channels if not isinstance(c, discord.CategoryChannel)}
    for ch_data in backup["channels"]:
        if ch_data["type"] == "category" or ch_data["name"] in existing_channel_names:
            continue
        category = category_name_to_category.get(ch_data["category"]) if ch_data["category"] else None
        overwrites = _rebuild_overwrites(ch_data["overwrites"], role_name_to_role)
        try:
            if ch_data["type"] == "text":
                await guild.create_text_channel(
                    ch_data["name"], category=category, topic=ch_data.get("topic"),
                    overwrites=overwrites, reason="Server backup restore",
                )
            else:
                await guild.create_voice_channel(
                    ch_data["name"], category=category, overwrites=overwrites, reason="Server backup restore",
                )
            created_channels += 1
        except discord.HTTPException:
            pass

    return created_roles, created_channels

