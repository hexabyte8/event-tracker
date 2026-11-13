import argparse
import logging

from .config import database_path, load_config
from .crawler import crawl
from .web import create_app


def main():
    parser = argparse.ArgumentParser(description="Find events in configured cities.")
    parser.add_argument("command", choices=["serve", "crawl"])
    parser.add_argument("--config", help="JSON configuration path (or EVENT_TRACKER_CONFIG)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "crawl":
        _, errors = crawl(load_config(args.config), database_path())
        return 1 if errors else 0
    create_app(args.config).run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
