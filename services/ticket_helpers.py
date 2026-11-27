"""Pure helpers for ticket topic metadata and panel lookup."""


def _ticket_topic_parts(topic):
    """Read legacy opener-only topics and the panel-aware topic format."""
    value = str(topic or "")
    if value.startswith("nexus-ticket|"):
        parts = {}
        for item in value.split("|")[1:]:
            if ":" in item:
                key, val = item.split(":", 1)
                parts[key] = val
        return parts.get("opener"), parts.get("panel"), parts.get("support"), parts.get("archive")
    return (value if value.isdigit() else None), None, None, None


def _ticket_topic_value(topic, key):
    """Return one metadata value from a Nexus ticket topic."""
    value = str(topic or "")
    if not value.startswith("nexus-ticket|"):
        return None
    for item in value.split("|")[1:]:
        if ":" in item:
            field, field_value = item.split(":", 1)
            if field == key:
                return field_value
    return None


def _find_ticket_panel(cfg, panel_id):
    if not panel_id:
        return None
    return next((p for p in (cfg.get("ticket_panels") or []) if str(p.get("id")) == str(panel_id)), None)


TICKET_PRIORITIES = {"low", "normal", "high", "urgent"}


def _ticket_topic_update(topic, key, value):
    """Update one metadata field without breaking legacy ticket topics."""
    current = str(topic or "")
    if current.startswith("nexus-ticket|"):
        items = current.split("|")
        found = False
        for index in range(1, len(items)):
            if items[index].split(":", 1)[0] == key:
                items[index] = f"{key}:{value}"
                found = True
                break
        if not found:
            items.append(f"{key}:{value}")
        return "|".join(items)
    opener = current if current.isdigit() else ""
    fields = [f"nexus-ticket|opener:{opener}"]
    if key != "opener":
        fields.append(f"{key}:{value}")
    return "|".join(fields)

