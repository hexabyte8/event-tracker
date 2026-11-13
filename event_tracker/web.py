from flask import Flask, render_template, request

from .config import database_path, load_config
from .store import database, find_events


def create_app(config_path=None, db_path=None):
    app = Flask(__name__)
    path = db_path or database_path()
    load_config(config_path)
    with database(path):
        pass

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https: http:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/healthz")
    def health():
        with database(path) as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/")
    def index():
        config = load_config(config_path)
        cities = [city["name"] for city in config["cities"]]
        city = request.args.get("city", "")
        query = request.args.get("q", "")[:200]
        include_past = request.args.get("past") == "1"
        events = find_events(path, cities, city, query, include_past)
        return render_template(
            "index.html", events=events, cities=cities, city=city, query=query, include_past=include_past
        )

    return app
