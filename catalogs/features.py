"""features -- liste des fonctionnalités activables par serveur (toggles).

Données pures (aucune dépendance) : partagées par les commandes /feature, le
dashboard web et la REST API de l'app mobile. Ajouter un toggle ici le fait
apparaître partout. Extrait de main.py (étape 2b-1).
"""

FEATURE_TOGGLES = {
    "detailed_logs": {"config_key": "detailed_logs_enabled", "label": "Detailed activity logs (voice, nicknames, roles)"},
    "antinuke": {"config_key": "antinuke_enabled", "label": "Anti-nuke protection"},
    "weekly_digest": {"config_key": "weekly_digest_enabled", "label": "Weekly DM digest"},
    "dashboard_logging": {"config_key": "log_dashboard_actions", "label": "Log dashboard actions to the logs channel"},
    "anti_link_global": {"config_key": "anti_link_global_enabled", "label": "Block ALL links, not just Discord invites"},
    "welcome_banner": {"config_key": "welcome_banner_enabled", "label": "Generate a welcome banner image on member join"},
    "verification": {"config_key": "verification_enabled", "label": "Assign Unverified role on join until member verifies"},
    "quarantine": {"config_key": "quarantine_enabled", "label": "Hold suspicious new accounts for review"},
    "anti_mention_spam": {"config_key": "anti_mention_spam_enabled", "label": "Delete messages with too many mentions / @everyone abuse"},
    "anti_media_flood": {"config_key": "anti_media_flood_enabled", "label": "Delete rapid-fire image/attachment spam"},
    "auto_slowmode": {"config_key": "auto_slowmode_enabled", "label": "Temporarily enable slowmode when a channel gets hyperactive"},
    "anti_doxx": {"config_key": "anti_doxx_enabled", "label": "Detect and act on phone numbers / address-like content"},
    "raid_pattern_alert": {"config_key": "raid_pattern_alert_enabled", "label": "Alert on coordinated-looking join patterns (similar usernames)"},
    "auto_translate": {"config_key": "auto_translate_enabled", "label": "Translate a message via 🌐 reaction (DMs the translation)"},
    "remindme": {"config_key": "remindme_enabled", "label": "Enable the /remindme command"},
    "serverinfo_visual": {"config_key": "serverinfo_visual_enabled", "label": "Attach a 7-day activity chart to /serverinfo"},
    "trap_channel": {"config_key": "trap_channel_enabled", "label": "Any message in the trap channel = instant softban (no content check)"},
}
