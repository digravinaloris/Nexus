"""MongoDB connection and collection initialization for the bot runtime.

The entrypoint owns the legacy module-level collection aliases for now. This
module centralizes the actual connection and stable core.col binding so that
initialization can move out of main.py without changing callers.
"""

from . import core


COLLECTION_BINDINGS = {
    "warns_col": "warns",
    "config_col": "config",
    "locked_channels_col": "locked_channels",
    "sanctions_col": "sanctions",
    "reaction_roles_col": "reaction_roles",
    "notes_col": "notes",
    "audit_col": "audit_log",
    "bot_state_col": "bot_state",
    "killswitch_tokens_col": "killswitch_tokens",
    "case_counters_col": "case_counters",
    "sticky_messages_col": "sticky_messages",
    "ban_appeals_col": "ban_appeals",
    "server_backups_col": "server_backups",
    "invite_uses_col": "invite_uses",
    "role_menus_col": "role_menus",
    "scheduled_col": "scheduled_announcements",
    "quarantine_col": "quarantine",
    "reminders_col": "reminders",
    "templates_col": "templates",
}


def initialize_mongo(client_factory, mongo_uri, bot):
    """Connect to MongoDB and return the legacy collection aliases.

    client_factory is injected so the entrypoint's existing MongoClient
    patch point remains compatible with tests and alternate runtimes.
    """
    mongo = client_factory(mongo_uri, serverSelectionTimeoutMS=5000)
    database = mongo["discordbot"]
    collections = {
        alias: database[collection]
        for alias, collection in COLLECTION_BINDINGS.items()
    }

    # Publish a complete collection set through the stable runtime object.
    core.init_db(mongo, database)

    # Cogs access bot.db only when handling an event/command, after on_ready.
    bot.db = database

    return {"mongo": mongo, "db": database, **collections}
