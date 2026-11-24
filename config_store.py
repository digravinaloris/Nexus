"""config_store -- configuration par serveur (collection `config`) et verrou global.

Extrait de main.py (étape 2a). N'importe JAMAIS main.py : les collections
viennent de `core.col`. main.py ré-importe ces noms, donc tout le code existant
(`get_config(...)`, `update_config(...)`) marche sans modification.
"""
from core import col


def get_config(guild_id):
    doc = col.config.find_one({"guild_id": str(guild_id)})
    if not doc:
        doc = {
            "guild_id": str(guild_id),
            "logs_channel": "logs",
            "autorole": None,
            "allowed_roles": [],
            "command_roles": {},
            "command_templates": {},
            "language": None,
            "log_dashboard_actions": False,
            "log_webhook_url": None,
            "log_webhook_channel_id": None,
            "tickets_enabled": False,
            "ticket_category_id": None,
            "ticket_support_role_id": None,
            "ticket_archive_category_id": None,
            "ticket_panels": [],
            "appeal_channel_id": None,
            "boost_channel_id": None,
            "detailed_logs_enabled": False,
            "banned_words": [],
            "antinuke_enabled": False,
            "antinuke_threshold": 5,
            "weekly_digest_enabled": False,
            "last_digest_sent_at": None,
            "member_count_channel_id": None,
            "min_account_age_days": 0,
            "banned_domains": [],
            "anti_link_global_enabled": False,
            "welcome_channel_id": None,
            "welcome_banner_enabled": False,
            "verification_enabled": False,
            "verified_role_id": None,
            "unverified_role_id": None,
            "quarantine_enabled": False,
            "quarantine_role_id": None,
            "quarantine_channel_id": None,
            "quarantine_hold_minutes": 30,
            "quarantine_suspicion_days": 7,
            "anti_mention_spam_enabled": False,
            "mention_spam_threshold": 5,
            "anti_media_flood_enabled": False,
            "media_flood_count": 5,
            "media_flood_window": 10,
            "auto_slowmode_enabled": False,
            "auto_slowmode_threshold": 10,
            "auto_slowmode_window": 10,
            "auto_slowmode_seconds": 10,
            "auto_slowmode_duration_minutes": 5,
            "anti_doxx_enabled": False,
            "raid_pattern_alert_enabled": False,
            "auto_translate_enabled": False,
            "remindme_enabled": True,
            "serverinfo_visual_enabled": False,
            "trap_channel_enabled": False,
            "trap_channel_id": None,
            "automod_spam_count": 10,
            "automod_spam_window": 5,
            "automod_caps_ratio": 0.7,
            "automod_raid_count": 5,
            "automod_raid_window": 10,
            "warn_escalation_enabled": False,
            "warn_mute_threshold": 3,
            "warn_mute_minutes": 10,
            "warn_kick_threshold": 5,
            "jtc_channel_id": None,
            "jtc_category_id": None,
            "pending_role_menu_items": [],
        }
        col.config.insert_one(doc)
    return doc


def update_config(guild_id, key, value):
    col.config.update_one({"guild_id": str(guild_id)}, {"$set": {key: value}}, upsert=True)


def add_command_role(guild_id, command_name, role_id):
    """Autorise un rôle à utiliser une commande spécifique pour ce serveur."""
    col.config.update_one(
        {"guild_id": str(guild_id)},
        {"$addToSet": {f"command_roles.{command_name}": role_id}},
        upsert=True,
    )


def remove_command_role(guild_id, command_name, role_id):
    """Retire l'autorisation d'un rôle pour une commande spécifique sur ce serveur."""
    col.config.update_one(
        {"guild_id": str(guild_id)},
        {"$pull": {f"command_roles.{command_name}": role_id}},
        upsert=True,
    )


def set_command_template(guild_id, template_name, commands_list):
    """Crée ou remplace un template de permissions propre à ce serveur."""
    col.config.update_one(
        {"guild_id": str(guild_id)},
        {"$set": {f"command_templates.{template_name}": commands_list}},
        upsert=True,
    )


def get_global_lock():
    doc = col.bot_state.find_one({"_id": "global"})
    return bool(doc and doc.get("locked"))


def set_global_lock(locked: bool):
    col.bot_state.update_one({"_id": "global"}, {"$set": {"locked": locked}}, upsert=True)
