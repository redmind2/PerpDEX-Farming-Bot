from __future__ import annotations

import argparse

from perpdex_farming_bot.gui.local_server import run_server
from perpdex_farming_bot.runtime_control import DEFAULT_RUNTIME_CONTROL_PATH
from perpdex_farming_bot.storage.settings_db import DEFAULT_SETTINGS_DB


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local PerpDEX Farming Bot web GUI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--settings-db", default=DEFAULT_SETTINGS_DB)
    parser.add_argument("--runtime-control", default=DEFAULT_RUNTIME_CONTROL_PATH)
    args = parser.parse_args()

    run_server(
        host=args.host,
        port=args.port,
        env_file=args.env_file,
        settings_db=args.settings_db,
        runtime_control_path=args.runtime_control,
    )


if __name__ == "__main__":
    main()
