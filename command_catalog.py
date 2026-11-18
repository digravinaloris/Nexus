"""command_catalog -- catalogue des commandes de modération et modèles de permissions prédéfinis.

Données pures partagées par les slash commands (/config allow...) et le dashboard web.
Extrait de main.py (étape 2b-2b).
"""


MODERATION_COMMANDS = [
    "ban", "kick", "mute", "unmute", "unban", "warn", "unwarn", "clear",
    "roleadd", "roleremove", "lock", "unlock", "vlock", "vunlock",
    "slowmode", "nickname", "softban", "purgeuser", "tempban", "broadcast",
]


# ---------- Modèles de permissions par commande ----------
# Des packs de commandes courants, pour éviter de tout cocher à la main
# à chaque fois qu'on crée un nouveau rôle de modération.
COMMAND_TEMPLATES = {
    "trial_mod": {"label_key": "tmpl_trial_mod", "commands": ["warn", "unwarn", "mute", "unmute", "clear"]},
    "moderator": {"label_key": "tmpl_moderator", "commands": [
        "warn", "unwarn", "mute", "unmute", "kick", "ban", "unban",
        "clear", "roleadd", "roleremove", "slowmode", "softban", "purgeuser", "tempban",
    ]},
    "helper": {"label_key": "tmpl_helper", "commands": ["warn", "clear", "slowmode"]},
    "dj": {"label_key": "tmpl_dj", "commands": ["play", "skip", "pause", "resume", "stop", "queue", "volume"]},
}
