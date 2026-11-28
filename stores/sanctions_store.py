"""sanctions_store -- avertissements, salons verrouillés, cases de sanction, appels.

Extrait de main.py (étape 2a). Couche données uniquement (aucune dépendance à
discord ni à Flask) ; les collections viennent de `core.col`. main.py ré-importe
ces noms pour que le code existant marche sans modification.
"""
import datetime

from pymongo import ReturnDocument

from nexus_runtime.core import col


def get_warns(guild_id, user_id):
    doc = col.warns.find_one({"guild_id": str(guild_id), "user_id": str(user_id)})
    return doc["count"] if doc else 0


def set_warns(guild_id, user_id, count):
    col.warns.update_one({"guild_id": str(guild_id), "user_id": str(user_id)}, {"$set": {"count": count}}, upsert=True)


def mark_channel_locked(guild_id, channel_id, channel_name, locked_by, channel_type="text"):
    col.locked_channels.update_one(
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
    col.locked_channels.delete_one({"guild_id": str(guild_id), "channel_id": str(channel_id)})


def get_locked_channels(guild_id):
    return list(col.locked_channels.find({"guild_id": str(guild_id)}))


def get_next_case_id(guild_id):
    """Compteur atomique par serveur — jamais de doublon même avec des
    sanctions concurrentes."""
    doc = col.case_counters.find_one_and_update(
        {"_id": str(guild_id)},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["seq"]


def log_sanction(guild_id, user_id, sanction_type, reason, moderator_id):
    """Enregistre une sanction dans l'historique. moderator_id peut être un ID Discord,
    'mobile_app' (action via l'app Android), ou 'automod' (déclenchée automatiquement).
    Retourne le case_id généré, consultable ensuite via /case view."""
    case_id = get_next_case_id(guild_id)
    col.sanctions.insert_one({
        "guild_id": str(guild_id),
        "case_id": case_id,
        "user_id": str(user_id),
        "type": sanction_type,
        "reason": reason or "No reason provided",
        "moderator_id": str(moderator_id),
        "timestamp": datetime.datetime.now(datetime.timezone.utc),
    })
    return case_id


def get_case(guild_id, case_id):
    return col.sanctions.find_one({"guild_id": str(guild_id), "case_id": case_id})


def create_sanction_appeal(guild_id, case_id, sanction_type, user_id, user_name, reason, token):
    col.ban_appeals.insert_one({
        "token": token,
        "guild_id": str(guild_id),
        "case_id": case_id,
        "sanction_type": sanction_type,
        "user_id": str(user_id),
        "user_name": user_name,
        "ban_reason": reason,  # nom de champ conservé pour compat avec les anciens enregistrements
        "status": "pending",  # pending -> submitted -> accepted / denied
        "appeal_text": None,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "submitted_at": None,
        "resolved_at": None,
    })


def get_ban_appeal(token):
    return col.ban_appeals.find_one({"token": token})



def claim_sanction_appeal(guild_id, token):
    """Atomically claim a submitted appeal so only one reviewer can resolve it."""
    result = col.ban_appeals.update_one(
        {"token": token, "guild_id": str(guild_id), "status": "submitted"},
        {"$set": {
            "status": "processing",
            "processing_at": datetime.datetime.now(datetime.timezone.utc),
        }},
    )
    return result.modified_count == 1


def resolve_sanction_appeal(guild_id, token, status):
    """Finalize an appeal claimed by this workflow."""
    if status not in ("accepted", "denied"):
        raise ValueError("Invalid appeal resolution status")
    result = col.ban_appeals.update_one(
        {"token": token, "guild_id": str(guild_id), "status": "processing"},
        {"$set": {
            "status": status,
            "resolved_at": datetime.datetime.now(datetime.timezone.utc),
        }, "$unset": {"processing_at": ""}},
    )
    return result.modified_count == 1


def release_sanction_appeal_claim(guild_id, token):
    """Return a claimed appeal to submitted if its resolution action failed."""
    result = col.ban_appeals.update_one(
        {"token": token, "guild_id": str(guild_id), "status": "processing"},
        {"$set": {"status": "submitted"}, "$unset": {"processing_at": ""}},
    )
    return result.modified_count == 1

def get_ban_appeal_by_case(guild_id, case_id):
    return col.ban_appeals.find_one({"guild_id": str(guild_id), "case_id": case_id})


def get_sanction_stats(guild_id, days=30):
    """Agrège le nombre de sanctions par jour sur les N derniers jours, pour
    le graphe du dashboard. Retourne une liste de (label_jour, nombre),
    avec les jours sans sanction inclus à zéro (sinon le graphe serait
    trompeur : il sauterait les périodes calmes)."""
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    counts = {}
    try:
        for doc in col.sanctions.find({"guild_id": str(guild_id), "timestamp": {"$gte": since}}):
            ts = doc["timestamp"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            key = ts.strftime("%Y-%m-%d")
            counts[key] = counts.get(key, 0) + 1
    except Exception as e:
        print(f"[STATS] Failed to aggregate sanctions: {e}", flush=True)
        return []

    today = datetime.datetime.now(datetime.timezone.utc).date()
    series = []
    for i in range(days - 1, -1, -1):
        day = today - datetime.timedelta(days=i)
        key = day.strftime("%Y-%m-%d")
        series.append((day.strftime("%d/%m"), counts.get(key, 0)))
    return series



def get_dashboard_warning_counts(guild_id, since):
    """Return active warned users and warning sanctions since a UTC timestamp."""
    guild_key = str(guild_id)
    warned_count = col.warns.count_documents({
        "guild_id": guild_key,
        "count": {"$gt": 0},
    })
    warns_today = col.sanctions.count_documents({
        "guild_id": guild_key,
        "type": "warn",
        "timestamp": {"$gte": since},
    })
    return {
        "warned_count": warned_count,
        "warns_today": warns_today,
    }


def get_warnlist(guild_id):
    """Return active warning counts for one guild, without exposing Mongo details."""
    return list(col.warns.find({"guild_id": str(guild_id), "count": {"$gt": 0}}))



def get_sanction_cases(guild_id, filter_type="", limit=50):
    """Return recent sanction cases for the mobile case browser."""
    query = {"guild_id": str(guild_id)}
    if filter_type:
        query["type"] = filter_type
    return list(col.sanctions.find(query).sort("timestamp", -1).limit(limit))



def get_submitted_appeals(guild_id, limit=100):
    """Return a bounded list of submitted appeals awaiting moderator review, newest first."""
    return list(
        col.ban_appeals.find({"guild_id": str(guild_id), "status": "submitted"})
        .sort("submitted_at", -1)
        .limit(limit)
    )



def get_sanction_types(guild_id):
    """Return the sanction types recorded for a single guild."""
    return col.sanctions.distinct("type", {"guild_id": str(guild_id)})



def get_sanction_types(guild_id):
    """Return the sanction types recorded for a single guild."""
    return col.sanctions.distinct("type", {"guild_id": str(guild_id)})




def submit_ban_appeal(token, appeal_text, submitted_at=None):
    """Atomically transition a pending public appeal to submitted."""
    submitted_at = submitted_at or datetime.datetime.now(datetime.timezone.utc)
    result = col.ban_appeals.update_one(
        {"token": token, "status": "pending"},
        {"$set": {
            "status": "submitted",
            "appeal_text": appeal_text,
            "submitted_at": submitted_at,
        }},
    )
    return result.modified_count == 1

def get_sanction_history(guild_id, user_id, limit=15):
    return list(
        col.sanctions.find({"guild_id": str(guild_id), "user_id": str(user_id)})
        .sort("timestamp", -1)
        .limit(limit)
    )


def save_tempban(guild_id, user_id, unban_at):
    col.sanctions.update_one(
        {"guild_id": str(guild_id), "user_id": str(user_id), "type": "tempban_active"},
        {"$set": {"unban_at": unban_at}},
        upsert=True,
    )


def remove_tempban(guild_id, user_id):
    col.sanctions.delete_one({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "type": "tempban_active",
    })


def get_active_tempbans():
    return list(col.sanctions.find({"type": "tempban_active"}))

def update_case_reason(guild_id, case_id, reason, editor_id, editor_name, edited_at):
    return col.sanctions.update_one(
        {"guild_id": str(guild_id), "case_id": case_id},
        {"$set": {
            "reason": reason,
            "edited": True,
            "edited_by": str(editor_id),
            "edited_by_name": editor_name,
            "edited_at": edited_at,
        }},
    )


def add_case_followup_note(guild_id, case_id, author_name, text, timestamp):
    return col.sanctions.update_one(
        {"guild_id": str(guild_id), "case_id": case_id},
        {"$push": {"followup_notes": {
            "author_name": author_name,
            "text": text,
            "timestamp": timestamp,
        }}},
    )


def delete_user_moderation_data(guild_id, user_id):
    query = {"guild_id": str(guild_id), "user_id": str(user_id)}
    col.warns.delete_many(query)
    col.sanctions.delete_many(query)
    col.notes.delete_many(query)


def list_guild_sanctions(guild_id):
    return list(
        col.sanctions.find({"guild_id": str(guild_id)}).sort("timestamp", -1)
    )


def list_sanctions_since(guild_id, since):
    return list(col.sanctions.find({
        "guild_id": str(guild_id),
        "timestamp": {"$gte": since},
    }))




def resolve_submitted_sanction_appeal(token, status, resolved_at=None):
    """Record an accepted or denied decision for an appeal."""
    if status not in ("accepted", "denied"):
        raise ValueError("Invalid appeal resolution status")
    resolved_at = resolved_at or datetime.datetime.now(datetime.timezone.utc)
    result = col.ban_appeals.update_one(
        {"token": token},
        {"$set": {"status": status, "resolved_at": resolved_at}},
    )
    return result.modified_count == 1
