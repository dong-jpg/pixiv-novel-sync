from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .auth import PixivAuthManager
from .logging_utils import configure_logging
from .settings import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pixiv-novel-sync")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config YAML")
    parser.add_argument("--env-file", default=None, help="Path to .env file")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("auth-check", help="Validate Pixiv auth settings")
    subparsers.add_parser("sync-bookmarks", help="Sync bookmarked novels")
    subparsers.add_parser("db-stats", help="Show database statistics")

    web_parser = subparsers.add_parser("web-token-ui", help="Start local web UI for acquiring Pixiv refresh_token")
    web_parser.add_argument("--host", default="127.0.0.1", help="Bind host for token UI")
    web_parser.add_argument("--port", default=5010, type=int, help="Bind port for token UI")
    web_parser.add_argument("--config", default="config/config.yaml", help="Path to config YAML")
    web_parser.add_argument("--env-file", default=None, help="Path to .env file")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    settings = load_settings(config_path=args.config, env_path=args.env_file)
    configure_logging(settings.log_level)

    if args.command == "auth-check":
        run_auth_check(args.config, args.env_file)
    elif args.command == "sync-bookmarks":
        from .jobs.quick_sync import run_bookmark_sync

        run_bookmark_sync(settings)
    elif args.command == "db-stats":
        from .storage_db import Database

        db = Database(settings.storage.db_path)
        db.init_schema()
        try:
            print(db.export_stats())
        finally:
            db.close()
    elif args.command == "web-token-ui":
        from .webapp import create_app
 
        app = create_app(config_path=args.config, env_path=args.env_file)
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
    else:
        parser.error(f"Unsupported command: {args.command}")


def run_auth_check(config_path: str | Path, env_path: str | Path | None) -> None:
    settings = load_settings(config_path=config_path, env_path=env_path)
    auth = PixivAuthManager(settings.pixiv)
    _, result = auth.login()

    output = {
        "user_id": result.user_id,
        "has_access_token": bool(result.access_token),
        "has_refresh_token": bool(result.refresh_token),
    }
    logging.getLogger(__name__).info("Auth check passed")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
