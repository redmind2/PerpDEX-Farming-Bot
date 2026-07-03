from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from perpdex_farming_bot.budget import current_weekly_window
from perpdex_farming_bot.cli.hibachi_weekly_live_test import (
    _build_runs,
    _dict,
    _load_json,
    _load_plan,
    _market_catalog,
    phase_target_totals,
)
from perpdex_farming_bot.connectors.hibachi_readonly import (
    DEFAULT_HIBACHI_API_ENDPOINT,
    DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    endpoint_from_env,
)
from perpdex_farming_bot.credentials import hibachi_available_credential_env, read_hibachi_credentials
from perpdex_farming_bot.env import get_env, load_dotenv_if_present
from perpdex_farming_bot.storage import WeeklyLedger


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only Hibachi weekly live ledger and optional private balance status.",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--weekly-config", default="config/hibachi.weekly.test.json")
    parser.add_argument("--accounts-config", default="config/accounts.json")
    parser.add_argument("--markets-config", default="config/markets.json")
    parser.add_argument(
        "--live-ledger-db",
        default=os.environ.get("PERPDEX_HIBACHI_LIVE_LEDGER_DB", "data/hibachi_live_ledger.sqlite"),
    )
    parser.add_argument(
        "--network-readonly",
        action="store_true",
        help="Also fetch private read-only balance/account info from Hibachi. Never places orders.",
    )
    args = parser.parse_args()

    load_dotenv_if_present(args.env_file)

    plan = _load_plan(Path(args.weekly_config))
    accounts = _load_json(Path(args.accounts_config))
    markets = _load_json(Path(args.markets_config))
    runs = _build_runs(plan, accounts, _market_catalog(markets))
    week = current_weekly_window(
        datetime.now(timezone.utc),
        str(_dict(plan.get("week")).get("start_weekday_utc", "monday")),
    )

    ledger = WeeklyLedger(args.live_ledger_db)
    ledger.init()

    exchange_id = str(plan.get("exchange_id", "hibachi"))
    targets = phase_target_totals(plan, runs)
    total = ledger.live_weekly_totals(week, exchange_id=exchange_id)

    print("hibachi_weekly_status=read_only_no_orders")
    print(f"weekly_config={args.weekly_config}")
    print(f"live_ledger_db={args.live_ledger_db}")
    print(f"period_start_utc={week.start_utc.isoformat()}")
    print(f"period_end_utc={week.end_utc.isoformat()}")
    print(f"fx_target_volume_usd={targets.fx_volume_usd:.2f}")
    print(f"crypto_target_volume_usd={targets.crypto_volume_usd:.2f}")
    print(f"combined_target_volume_usd={targets.combined_volume_usd:.2f}")
    print(f"live_weekly_planned_gross_volume_usd={total.planned_gross_volume_usd:.2f}")
    print(f"live_weekly_recorded_round_count={total.run_count}")
    print("live_volume_source=local_bot_ledger_successful_confirmed_rounds")

    if runs:
        print("phase_status:")
    for run in runs:
        run_total = ledger.live_weekly_totals(
            week,
            exchange_id=run.exchange_id,
            account_group_key=run.wallet.group_key,
            wallet_key=run.wallet.wallet_key,
            market=run.market,
        )
        print(
            f"- phase={run.phase_label} account_group={run.wallet.group_key} "
            f"wallet_role={run.wallet.role} wallet={run.wallet.wallet_key} "
            f"market={run.market} monitored=True "
            f"recorded_market_volume_usd={run_total.planned_gross_volume_usd:.2f} "
            f"recorded_rounds={run_total.run_count}"
        )

    rows = ledger.live_weekly_market_totals(week, exchange_id=exchange_id)
    if rows:
        print("recorded_market_totals:")
        for row in rows:
            print(
                f"- account_group={row.account_group_key} wallet_role={row.wallet_role} "
                f"wallet={row.wallet_key} market={row.market} "
                f"planned_gross_volume_usd={row.planned_gross_volume_usd:.2f} "
                f"round_count={row.run_count}"
            )

    if not args.network_readonly:
        print("private_readonly_balance=skipped")
        print("points_status=skipped_without_network_readonly")
        return

    print("private_readonly_balance=enabled")
    for prefix in _credential_prefixes(runs):
        print(_private_wallet_summary(prefix))


def _credential_prefixes(runs: list[object]) -> list[str]:
    prefixes: list[str] = []
    for run in runs:
        prefix = str(run.wallet.credential_prefix)
        if prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def _private_wallet_summary(credential_prefix: str) -> str:
    if hibachi_available_credential_env(credential_prefix) is None:
        return "\n".join(
            (
                f"wallet_credential_prefix={credential_prefix}",
                "private_balance_status=missing_credentials",
                "points_status=unavailable",
            )
        )

    try:
        from hibachi_xyz import HibachiApiClient
    except ImportError:
        return "\n".join(
            (
                f"wallet_credential_prefix={credential_prefix}",
                "private_balance_status=hibachi_xyz_not_installed",
                "points_status=unavailable",
            )
        )

    credentials = read_hibachi_credentials(credential_prefix)
    client = HibachiApiClient(
        api_url=endpoint_from_env(get_env("HIBACHI_API_ENDPOINT_PRODUCTION"), DEFAULT_HIBACHI_API_ENDPOINT),
        data_api_url=endpoint_from_env(
            get_env("HIBACHI_DATA_API_ENDPOINT_PRODUCTION"),
            DEFAULT_HIBACHI_DATA_API_ENDPOINT,
        ),
        api_key=credentials["api_key"],
        account_id=credentials["account_id"],
        private_key=credentials["private_key"],
    )
    account_info = client.get_account_info()
    capital_balance = client.get_capital_balance()
    points_status, points_value = _extract_points(account_info, capital_balance)
    return "\n".join(
        (
            f"wallet_credential_prefix={credential_prefix}",
            "private_balance_status=ok",
            f"account_balance={getattr(account_info, 'balance', 'missing')}",
            f"capital_balance={getattr(capital_balance, 'balance', 'missing')}",
            f"positions_count={len(getattr(account_info, 'positions', ()))}",
            f"assets_count={len(getattr(account_info, 'assets', ()))}",
            f"points_status={points_status}",
            f"points_value={points_value}",
        )
    )


def _extract_points(*objects: object) -> tuple[str, str]:
    candidate_names = (
        "points",
        "point",
        "totalPoints",
        "total_points",
        "rewardPoints",
        "reward_points",
        "makerPoints",
        "maker_points",
        "takerPoints",
        "taker_points",
    )
    for item in objects:
        for name in candidate_names:
            value = getattr(item, name, None)
            if value not in (None, ""):
                return f"found_field:{name}", str(value)
    return "not_found_in_hibachi_sdk_account_info", "unavailable"


if __name__ == "__main__":
    main()
