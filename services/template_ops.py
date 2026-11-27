"""template_ops -- application de la partie portable d'un template de configuration.

Partagé par /template load (Discord) et l'import depuis la marketplace web.
Extrait de main.py (étape 2b-2b).
"""
import math

from config_store import update_config
from catalogs.features import FEATURE_TOGGLES


TEMPLATE_AUTOMOD_KEYS = frozenset({
    "banned_words",
    "banned_domains",
    "automod_spam_count",
    "automod_spam_window",
    "automod_caps_ratio",
    "automod_raid_count",
    "automod_raid_window",
})

TEMPLATE_AUTOMOD_BOUNDS = {
    "automod_spam_count": (1, 1000),
    "automod_spam_window": (1, 3600),
    "automod_caps_ratio": (0.1, 1),
    "automod_raid_count": (2, 1000),
    "automod_raid_window": (2, 3600),
}


def _apply_template_base_config(guild_id, snapshot):
    """Apply only the portable, explicitly supported part of a template.

    Template documents are persisted data, so never use arbitrary keys from
    their payload as configuration paths.
    """
    if not isinstance(snapshot, dict):
        return

    feature_toggles = snapshot.get("feature_toggles", {})
    if isinstance(feature_toggles, dict):
        for key, enabled in feature_toggles.items():
            if key in FEATURE_TOGGLES and isinstance(enabled, bool):
                update_config(guild_id, FEATURE_TOGGLES[key]["config_key"], enabled)

    automod = snapshot.get("automod", {})
    if isinstance(automod, dict):
        for key, value in automod.items():
            if key not in TEMPLATE_AUTOMOD_KEYS or value is None:
                continue
            if key in {"banned_words", "banned_domains"}:
                if isinstance(value, list) and all(isinstance(item, str) for item in value):
                    update_config(guild_id, key, value)
            elif key in TEMPLATE_AUTOMOD_BOUNDS:
                minimum, maximum = TEMPLATE_AUTOMOD_BOUNDS[key]
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and minimum <= value <= maximum
                    and math.isfinite(value)
                ):
                    update_config(guild_id, key, value)

    warn_escalation_enabled = snapshot.get("warn_escalation_enabled", False)
    if isinstance(warn_escalation_enabled, bool):
        update_config(guild_id, "warn_escalation_enabled", warn_escalation_enabled)
