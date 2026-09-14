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
