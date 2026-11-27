"""FAQ persistence helpers shared by REST routes and bot FAQ commands."""
from nexus_runtime.core import col


def get_faq(guild_id):
    """Fetch one guild's FAQ document without creating defaults."""
    return col.faq.find_one({"guild_id": str(guild_id)})


def add_faq_entry(guild_id, entry):
    """Append a prepared FAQ entry; initialize the document if necessary."""
    doc = get_faq(guild_id) or {"guild_id": str(guild_id), "enabled": False, "entries": []}
    entries = doc.get("entries", [])
    entries.append(entry)
    col.faq.update_one({"guild_id": str(guild_id)}, {"$set": {"entries": entries}}, upsert=True)


def remove_faq_entry(guild_id, entry_id):
    """Remove an FAQ entry and report whether it existed."""
    doc = get_faq(guild_id)
    if not doc:
        return False
    entries = doc.get("entries", [])
    remaining = [entry for entry in entries if entry.get("id") != entry_id]
    if len(remaining) == len(entries):
        return False
    col.faq.update_one({"guild_id": str(guild_id)}, {"$set": {"entries": remaining}})
    return True



def set_faq_enabled(guild_id, enabled):
    """Set FAQ auto-help enabled state, creating a minimal document if absent."""
    col.faq.update_one({"guild_id": str(guild_id)}, {"$set": {"enabled": enabled}}, upsert=True)



def get_or_create_faq(guild_id):
    """Fetch or create a guild FAQ document with the established default fields."""
    doc = get_faq(guild_id)
    if doc is None:
        doc = {"guild_id": str(guild_id), "enabled": False, "entries": []}
        col.faq.insert_one(doc)
    return doc



def save_faq_entries(guild_id, entries):
    """Persist the FAQ entries list, preserving other document fields."""
    col.faq.update_one({"guild_id": str(guild_id)}, {"$set": {"entries": entries}}, upsert=True)
