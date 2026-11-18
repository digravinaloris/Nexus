"""template_ops -- application de la partie portable d'un template de configuration.

Partagé par /template load (Discord) et l'import depuis la marketplace web.
Extrait de main.py (étape 2b-2b).
"""
from config_store import update_config
from features import FEATURE_TOGGLES


def _apply_template_base_config(guild_id, snapshot):
    """Applique la partie 100% portable (toggles/automod/warn escalation) --
    jamais les rôles, qui ont toujours besoin d'un remapping explicite.
    Partagé par /template load (Discord) et l'import depuis la marketplace web."""
    for key, enabled in snapshot.get("feature_toggles", {}).items():
        if key in FEATURE_TOGGLES:
            update_config(guild_id, FEATURE_TOGGLES[key]["config_key"], enabled)
    for key, value in snapshot.get("automod", {}).items():
        if value is not None:
            update_config(guild_id, key, value)
    update_config(guild_id, "warn_escalation_enabled", snapshot.get("warn_escalation_enabled", False))
