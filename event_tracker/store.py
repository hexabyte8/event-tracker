import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import location_city


@contextmanager
def database(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                city TEXT NOT NULL,
                title TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                description TEXT NOT NULL,
                location TEXT NOT NULL,
                image_url TEXT NOT NULL,
                event_url TEXT NOT NULL,
                source_url TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(city, title, start_date, event_url)
            )
        """)
        yield connection
        connection.commit()
    finally:
        connection.close()


def save_events(path, events):
    with database(path) as connection:
        connection.executemany("""
            INSERT INTO events
                (city, title, start_date, end_date, description, location,
                 image_url, event_url, source_url)
            VALUES (:city, :title, :start_date, :end_date, :description, :location,
                    :image_url, :event_url, :source_url)
            ON CONFLICT(city, title, start_date, event_url) DO UPDATE SET
                end_date=excluded.end_date,
                description=excluded.description,
                location=excluded.location,
                image_url=excluded.image_url,
                source_url=excluded.source_url,
                updated_at=CURRENT_TIMESTAMP
        """, events)


def find_events(path, cities, city="", query="", include_past=False, location_cities=None):
    if city and city not in cities:
        return []
    selected = [city] if city else cities
    if not selected:
        return []
    stored_cities = cities if location_cities is not None else selected
    placeholders = ",".join("?" for _ in stored_cities)
    sql = f"SELECT * FROM events WHERE city IN ({placeholders})"
    parameters = list(stored_cities)
    if query:
        sql += " AND (instr(lower(title), lower(?)) > 0 OR instr(lower(description), lower(?)) > 0)"
        parameters += [query, query]
    if not include_past:
        sql += " AND (start_date = '' OR substr(COALESCE(NULLIF(end_date, ''), start_date), 1, 10) >= date('now'))"
    sql += " ORDER BY start_date = '', start_date, title"
    if location_cities is None:
        sql += " LIMIT 500"
    with database(path) as connection:
        results, seen = [], set()
        for row in connection.execute(sql, parameters):
            event = dict(row)
            if location_cities is not None:
                event["city"] = location_city(event["location"], location_cities)
                if event["city"] not in selected:
                    continue
                identity = (event["city"], event["title"], event["start_date"], event["event_url"])
                if identity in seen:
                    continue
                seen.add(identity)
            results.append(event)
            if len(results) == 500:
                break
        return results
