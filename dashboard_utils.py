"""Pure validation helpers for dashboard form data."""

from utils import parse_bounded_number


def read_bounded_form_numbers(values, fields):
    """Parse bounded numeric form values, ignoring blanks.

    fields contains (key, type, minimum, maximum) tuples. Invalid values
    raise ValueError with the offending field name, matching dashboard routes.
    """
    parsed = {}
    for key, kind, minimum, maximum in fields:
        raw = values.get(key, "")
        if not isinstance(raw, str):
            raw = str(raw)
        if not raw.strip():
            continue
        try:
            parsed[key] = parse_bounded_number(raw, kind, minimum, maximum)
        except ValueError as exc:
            raise ValueError(key) from exc
    return parsed
