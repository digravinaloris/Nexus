"""
Fonctions utilitaires pures (aucune dépendance à discord.py, Flask, ou
MongoDB) -- séparées de main.py pour pouvoir être testées sans avoir à
démarrer le bot ou se connecter à une base de données.
"""
import re

URL_RE = re.compile(r"https?://([a-zA-Z0-9_\-\.]+)", re.IGNORECASE)

CAPS_MIN_LENGTH = 10
CAPS_RATIO_THRESHOLD = 0.7


def parse_duration(duration_str: str) -> int:
    """Convertit une durée humaine ('1h', '2d', '30m', '1w') en secondes. Retourne -1 si invalide."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    match = re.fullmatch(r"(\d+)([smhdw])", duration_str.strip().lower())
    if not match:
        return -1
    return int(match.group(1)) * units[match.group(2)]


def check_caps(content, cfg=None):
    """Détecte un message majoritairement en majuscules. cfg (optionnel) peut
    fournir un seuil personnalisé via automod_caps_ratio."""
    ratio_threshold = cfg.get("automod_caps_ratio", CAPS_RATIO_THRESHOLD) if cfg else CAPS_RATIO_THRESHOLD
    letters = [c for c in content if c.isalpha()]
    if len(content) < CAPS_MIN_LENGTH or len(letters) < CAPS_MIN_LENGTH:
        return False
    upper_count = sum(1 for c in letters if c.isupper())
    return (upper_count / len(letters)) > ratio_threshold


def check_banned_words(content, banned_words):
    """Détection simple par mot entier (insensible à la casse) — évite de
    flag un mot innocent qui contiendrait juste une sous-chaîne interdite."""
    if not banned_words:
        return False
    lowered = content.lower()
    for word in banned_words:
        if re.search(r"\b" + re.escape(word.lower()) + r"\b", lowered):
            return True
    return False


def check_banned_domains(content, banned_domains):
    """Extrait les domaines des liens présents dans le message et les compare
    à la liste noire (avec sous-domaines : 'evil.com' bloque aussi 'sub.evil.com')."""
    if not banned_domains:
        return False
    found_domains = {m.group(1).lower() for m in URL_RE.finditer(content)}
    for domain in found_domains:
        for banned in banned_domains:
            banned = banned.lower()
            if domain == banned or domain.endswith("." + banned):
                return True
    return False


def check_any_link(content):
    """Détecte n'importe quel lien http(s) dans le message (contrairement à
    check_banned_domains, qui ne matche que les domaines d'une liste noire).
    Utilisé par le feature toggle anti_link_global (/feature enable anti_link_global)."""
    return bool(URL_RE.search(content))


# Amorces de question courantes, FR + EN (le bot et ses serveurs sont bilingues).
_QUESTION_STARTERS = (
    "comment", "pourquoi", "est-ce que", "est ce que", "c'est quoi", "c'est quand",
    "c'est où", "quoi", "qui est", "où est", "où se", "quand est", "combien",
    "peut-on", "peut on", "pouvez-vous", "pouvez vous", "quel", "quelle",
    "how", "why", "what", "when", "where", "who", "which", "can you", "could you",
    "is there", "are there", "do you", "does",
)


def looks_like_question(content):
    """Détection simple (pas de NLP) : un message compte comme une question
    s'il contient un '?' ou commence par une amorce de question FR/EN connue."""
    if "?" in content:
        return True
    lowered = content.strip().lower()
    return lowered.startswith(_QUESTION_STARTERS)


def match_faq_entries(content, entries):
    """Retourne la liste des entrées FAQ (dicts avec une clé 'keywords') dont
    au moins un mot-clé apparaît en mot entier dans le message. Ordre d'entrée
    conservé -- utilisé par le cog FAQ pour savoir combien de réponses matchent."""
    if not entries:
        return []
    lowered = content.lower()
    matches = []
    for entry in entries:
        for keyword in entry.get("keywords", []):
            keyword = keyword.strip().lower()
            if keyword and re.search(r"\b" + re.escape(keyword) + r"\b", lowered):
                matches.append(entry)
                break
    return matches


def format_uptime(total_seconds):
    """Formate un nombre de secondes en chaîne lisible ('2d 5h', '14m')."""
    total_seconds = int(total_seconds)
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "0m"
