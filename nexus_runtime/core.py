"""core -- état partagé de Nexus : l'accès à MongoDB.

Pourquoi ce module existe
-------------------------
Dans main.py, les collections Mongo (warns_col, config_col...) sont des
variables globales que `init_mongo()` réassigne au démarrage (dans on_ready).
Un autre module qui ferait `from main import warns_col` récupérerait `None`
pour toujours (la valeur est copiée à l'import, avant l'init). `core` règle ça :
les collections vivent dans UN objet stable (`core.col`) dont les attributs sont
remplis une fois la base connectée.

    from core import col          # sûr à n'importe quel moment, même à l'import
    col.warns.find_one({...})     # résolu au moment de l'appel, après init_mongo()

Avant l'init, `col.warns` lève `NotInitialized` avec un message clair (au lieu
d'un obscur "'NoneType' object has no attribute 'find_one'").

Règles
------
- `core` n'importe JAMAIS main.py (ni discord, ni flask) : zéro import circulaire,
  testable sans aucune dépendance lourde.
- main.py garde ses variables `xxx_col` pour l'instant (migration progressive,
  module par module). Un test vérifie que `core.col` et ces variables pointent
  toujours vers les mêmes collections (anti-dérive).
- Pour ajouter une collection : la déclarer dans COLLECTION_NAMES ET l'assigner
  dans init_mongo() (le test de parité échoue sinon).
"""

DB_NAME = "discordbot"

# attribut de `col`  ->  nom de la collection dans MongoDB
COLLECTION_NAMES = {
    "warns": "warns",
    "config": "config",
    "locked_channels": "locked_channels",
    "sanctions": "sanctions",
    "reaction_roles": "reaction_roles",
    "notes": "notes",
    "audit": "audit_log",
    "bot_state": "bot_state",
    "killswitch_tokens": "killswitch_tokens",
    "case_counters": "case_counters",
    "sticky_messages": "sticky_messages",
    "ban_appeals": "ban_appeals",
    "server_backups": "server_backups",
    "invite_uses": "invite_uses",
    "role_menus": "role_menus",
    "scheduled": "scheduled_announcements",
    "quarantine": "quarantine",
    "reminders": "reminders",
    "templates": "templates",  # GLOBALE : PAS scopée par guild_id, contrairement au reste
    "faq": "faq",              # utilisée par cogs/faq.py (via bot.db)
    "verification_challenges": "verification_challenges",  # liens de vérification Turnstile (core.col uniquement)
}


class NotInitialized(RuntimeError):
    """MongoDB n'est pas encore connecté (init_mongo() n'a pas tourné)."""


class Collections:
    """Accès par attribut aux collections : `col.warns`, `col.audit`, ..."""

    def __init__(self):
        self._bound = {}

    def _bind(self, database):
        # Un seul assignement du dict final : un thread qui lit (Flask) voit
        # soit l'ancien état complet, soit le nouveau, jamais un mélange.
        self._bound = {attr: database[name] for attr, name in COLLECTION_NAMES.items()}

    def _unbind(self):
        self._bound = {}

    @property
    def ready(self):
        return bool(self._bound)

    def __getattr__(self, attr):
        # N'est appelé que si l'attribut n'existe pas déjà sur l'objet.
        if attr.startswith("_") or attr not in COLLECTION_NAMES:
            raise AttributeError(f"core.col n'a pas de collection '{attr}'")
        try:
            return self._bound[attr]
        except KeyError:
            raise NotInitialized(
                f"core.col.{attr} utilisé avant la connexion MongoDB "
                f"(init_mongo() n'a pas encore tourné)"
            ) from None

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(COLLECTION_NAMES))


col = Collections()

bot = None    # commands.Bot, branché par main.py (core.bot = bot)
mongo = None  # MongoClient
db = None     # base `discordbot`


def init_db(mongo_client, database):
    """Branche core sur une base déjà ouverte. Appelé par main.init_mongo().

    Rappelable (on_ready peut se redéclencher à la reconnexion Discord) :
    chaque appel remplace proprement les collections.
    """
    global mongo, db
    col._bind(database)
    mongo = mongo_client
    db = database


def is_ready():
    return col.ready


def reset():
    """Remet core à l'état « non initialisé » (utilisé par les tests)."""
    global mongo, db
    col._unbind()
    mongo = None
    db = None
