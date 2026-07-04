from __future__ import annotations

import argparse

from perpdex_farming_bot.storage.settings_db import DEFAULT_SETTINGS_DB, SettingsDB


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local non-secret SQLite settings.")
    parser.add_argument("--db", default=DEFAULT_SETTINGS_DB)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the local settings DB schema.")

    import_parser = subparsers.add_parser("import-json", help="Import one non-secret JSON config file.")
    import_parser.add_argument("--path", required=True)
    import_parser.add_argument("--namespace", required=True)

    list_parser = subparsers.add_parser("list", help="List settings in one namespace.")
    list_parser.add_argument("--namespace", required=True)

    args = parser.parse_args()
    db = SettingsDB(args.db)
    db.init()

    if args.command == "init":
        print("settings_db_ready=True")
        print(f"settings_db={args.db}")
        return

    if args.command == "import-json":
        count = db.import_json_file(args.path, args.namespace)
        print("settings_imported=True")
        print(f"settings_db={args.db}")
        print(f"source_path={args.path}")
        print(f"namespace={args.namespace}")
        print(f"imported_key_count={count}")
        return

    if args.command == "list":
        rows = db.list_namespace(args.namespace)
        print(f"settings_db={args.db}")
        print(f"namespace={args.namespace}")
        print(f"row_count={len(rows)}")
        for row in rows:
            print(f"setting={row.namespace}.{row.key} updated_at={row.updated_at_utc}")
        return

    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
