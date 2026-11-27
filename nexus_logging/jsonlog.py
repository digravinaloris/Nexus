"""jsonlog -- log structuré en une ligne JSON.

Extrait de main.py (PR bienvenue) pour être partagé par les modules sans `import main`.
"""
import datetime
import json as _json


def log_json(level, event, **fields):
    """Log structuré en une ligne JSON -- Render capture stdout tel quel, donc
    une ligne JSON devient greppable/filtrable dans ses logs sans rien de plus
    à configurer côté infra. N'écrit jamais rien d'autre que cette ligne.
    Migration progressive depuis print(...) -- voir le changelog pour la liste
    des points déjà convertis ; le reste des print() existants n'a pas été
    touché pour ne rien risquer de casser sans pouvoir tester en prod."""
    payload = {"level": level, "event": event, "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), **fields}
    print(_json.dumps(payload, default=str), flush=True)
