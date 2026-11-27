"""audit -- journal d'audit du dashboard, fil d'activité et stats de sanctions.

Extrait de main.py (étape 2a). Les collections viennent de `core.col` ; l'accès
au bot (cache d'utilisateurs) passe par `core.bot`, jamais par `import main`
(sous `python main.py` le module s'appelle `__main__` : l'importer le
rechargerait une 2e fois, avec un 2e bot).
"""
import datetime

import core
from core import col


def record_audit(guild_id, actor_id, actor_name, action, details=""):
    """Trace de chaque changement de config (dashboard ou commande) pour
    l'audit log affiché dans le dashboard — indépendant du log Discord
    optionnel (log_dashboard_actions)."""
    try:
        col.audit.insert_one({
            "guild_id": str(guild_id),
            "actor_id": str(actor_id) if actor_id else None,
            "actor_name": actor_name,
            "action": action,
            "details": details[:500] if details else "",
            "timestamp": datetime.datetime.now(datetime.timezone.utc),
        })
    except Exception as e:
        print(f"[AUDIT] Failed to record audit entry: {e}", flush=True)


from audit_helpers.visuals import build_sparkline_svg


def get_audit_log(guild_id, limit=20):
    """Fil d'activité multi-source : fusionne les changements faits depuis
    le dashboard (col.audit) et les sanctions prises via les commandes
    (col.sanctions), triés chronologiquement — une vraie vue multi-admin,
    pas juste les actions web."""
    entries = []
    try:
        entries.extend(col.audit.find({"guild_id": str(guild_id)}).sort("timestamp", -1).limit(limit))
    except Exception as e:
        print(f"[AUDIT] Failed to fetch audit log: {e}", flush=True)

    try:
        for doc in col.sanctions.find({"guild_id": str(guild_id)}).sort("timestamp", -1).limit(limit):
            mod_id = doc.get("moderator_id", "")
            if mod_id == "mobile_app":
                actor_name = "📱 Mobile App"
            elif mod_id == "automod":
                actor_name = "🤖 AutoMod"
            else:
                try:
                    cached_user = core.bot.get_user(int(mod_id))
                    actor_name = str(cached_user) if cached_user else f"Moderator {mod_id}"
                except (ValueError, TypeError):
                    actor_name = f"Moderator {mod_id}"
            entries.append({
                "actor_name": actor_name,
                "action": f"{doc.get('type', 'sanction').replace('_', ' ').title()} (case #{doc.get('case_id', '?')})",
                "details": doc.get("reason", ""),
                "timestamp": doc["timestamp"],
            })
    except Exception as e:
        print(f"[AUDIT] Failed to fetch sanctions for activity feed: {e}", flush=True)

    def _sort_key(entry):
        ts = entry["timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return ts

    entries.sort(key=_sort_key, reverse=True)
    return entries[:limit]
