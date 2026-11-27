"""i18n -- traductions du dashboard (fichiers locales/en_us.yml / locales/fr_fr.yml) et helpers de session.

Extrait de main.py (étape 2a). `t()` lit la langue dans la session Flask.
"""
import os

import yaml
from flask import session


TRANSLATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")


_FALLBACK_TRANSLATIONS = {
    "en": {"logout": "Log out", "dash_title": "Your servers"},
    "fr": {"logout": "Se d\u00e9connecter", "dash_title": "Tes serveurs"},
}


def _load_locale_file(filename):
    """Charge un fichier de langue .yml. En cas de souci (fichier manquant,
    YAML invalide...), on log l'erreur et on retombe sur un mini-dict de
    secours plut\u00f4t que de planter tout le dashboard."""
    path = os.path.join(TRANSLATIONS_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        print(f"[I18N] Locale file not found: {path}", flush=True)
        return {}
    except yaml.YAMLError as e:
        print(f"[I18N] Failed to parse {path}: {e}", flush=True)
        return {}


TRANSLATIONS = {
    "en": _load_locale_file("en_us.yml") or _FALLBACK_TRANSLATIONS["en"],
    "fr": _load_locale_file("fr_fr.yml") or _FALLBACK_TRANSLATIONS["fr"],
}


def t(key):
    lang = session.get("lang", "en")
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, TRANSLATIONS["en"].get(key, key))


def current_lang():
    return session.get("lang", "en")


def current_theme():
    return session.get("theme", "dark")
