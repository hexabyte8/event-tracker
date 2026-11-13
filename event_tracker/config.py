import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def web_url(value, base=None):
    from urllib.parse import urljoin

    if not isinstance(value, str) or not value or "\\" in value:
        return ""
    try:
        url = urlsplit(urljoin(base, value) if base else value)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            return ""
        url.port
        return urlunsplit((url.scheme, url.netloc, url.path or "/", url.query, ""))
    except ValueError:
        return ""


def load_config(path=None):
    path = Path(path or os.environ.get("EVENT_TRACKER_CONFIG", "config.json"))
    with path.open(encoding="utf-8") as file:
        config = json.load(file)
    cities = config.get("cities")
    if not isinstance(cities, list) or not cities:
        raise ValueError("Configure at least one city in 'cities'.")
    names = set()
    for city in cities:
        if not isinstance(city, dict):
            raise ValueError("Each city must be an object.")
        name = city.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("City names must be nonempty and unique.")
        names.add(name)
        if not isinstance(city.get("sources"), list):
            raise ValueError(f"{name}: 'sources' must be a list of seed URLs.")
        for source in city["sources"]:
            if not web_url(source):
                raise ValueError(f"{name}: invalid HTTP(S) source URL.")
    for key, default, low, high in (
        ("max_pages_per_source", 25, 1, 100),
        ("max_depth", 1, 0, 3),
        ("request_delay_seconds", 1, 1, 60),
    ):
        value = config.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number.")
        if not low <= value <= high or (key != "request_delay_seconds" and int(value) != value):
            raise ValueError(f"{key} must be between {low} and {high}.")
    return config


def database_path():
    return os.environ.get("EVENT_TRACKER_DB", "data/events.sqlite3")
