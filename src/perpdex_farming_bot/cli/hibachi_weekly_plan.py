from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from perpdex_farming_bot.budget import current_weekly_window
from perpdex_farming_bot.credentials import hibachi_credential_env
from perpdex_farming_bot.env import load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.security.secrets import assert_no_plaintext_secrets


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only Hibachi weekly FX multiplier + Crypto volume plan check.",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--weekly-config", default="config/hibachi.weekly.json")
    parser.add_argument("--accounts-config", default="config/accounts.json")
    parser.add_argument("--markets-config", default="config/markets.json")
    parser.add_argument("--check-env", action="store_true")
    parser.add_argument("--network", action="store_true", help="Public read-only Hibachi inventory symbol check.")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    plan = _load_plan(Path(args.weekly_config))
    accounts = _load_json(Path(args.accounts_config))
    markets = _load_json(Path(args.markets_config))

    account_group_by_key = {
        str(item.get("account_group_key")): item
        for item in accounts.get("account_groups", ())
        if isinstance(item, dict) and item.get("account_group_key")
    }
    market_by_symbol = {
        (str(item.get("exchange_id")), str(item.get("market"))): item
        for item in markets.get("markets", ())
        if isinstance(item, dict) and item.get("exchange_id") and item.get("market")
    }

    exchange_id = str(plan.get("exchange_id", "hibachi"))
    week_config = _dict(plan.get("week"))
    week = current_weekly_window(datetime.now(timezone.utc), str(week_config.get("start_weekday_utc", "monday")))

    print("hibachi_weekly_plan=read_only_no_orders")
    print(f"env_file_loaded={env_loaded}")
    print(f"exchange_id={exchange_id}")
    print(f"week_timezone={week_config.get('timezone', 'UTC')}")
    print(f"period_start_weekday_utc={week_config.get('start_weekday_utc', 'monday')}")
    print(f"period_start_utc={week.start_utc.isoformat()}")
    print(f"period_end_utc={week.end_utc.isoformat()}")
    print("live_orders_enabled=False")

    _print_account_groups(plan, account_group_by_key, check_env=args.check_env)
    _print_execution_sizing(plan)
    _print_fx_phase(exchange_id, plan, market_by_symbol)
    _print_crypto_phase(exchange_id, plan, market_by_symbol)

    if args.network:
        _print_public_inventory_check(exchange_id, plan)

    print("hibachi_weekly_plan_ready=True")


def _load_plan(path: Path) -> dict[str, Any]:
    raw = _load_json(path)
    assert_no_plaintext_secrets(raw)
    return raw


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _print_account_groups(
    plan: dict[str, Any],
    account_group_by_key: dict[str, dict[str, Any]],
    *,
    check_env: bool,
) -> None:
    enabled_count = 0
    for group_ref in _plan_account_group_refs(plan):
        enabled = bool(group_ref.get("enabled", False))
        account_group_key = str(group_ref.get("account_group_key", ""))
        account_group = account_group_by_key.get(account_group_key)
        catalog_status = "ok" if account_group else "missing"
        catalog_enabled = bool(account_group.get("enabled", False)) if account_group else False
        effective_group_enabled = enabled and catalog_enabled
        if effective_group_enabled:
            enabled_count += 1
        print(
            f"account_group={account_group_key} enabled={enabled} "
            f"catalog_status={catalog_status} catalog_enabled={catalog_enabled}"
        )
        if account_group is None:
            continue
        wallets = _dict(account_group.get("wallets"))
        for role in ("crypto", "fx"):
            wallet = _dict(wallets.get(role))
            if not wallet:
                print(f"  {role}_wallet catalog_status=missing")
                continue
            prefix = str(wallet.get("credential_env_prefix", ""))
            legacy_prefixes = [str(item) for item in wallet.get("legacy_credential_env_prefixes", ()) if item]
            permissions = _dict(wallet.get("permissions"))
            wallet_enabled = bool(wallet.get("enabled", False))
            credential_status = _credential_status(prefix, legacy_prefixes) if check_env else "not_checked"
            effective_wallet_enabled = effective_group_enabled and wallet_enabled and (
                not check_env or credential_status.startswith("usable")
            )
            print(
                f"  {role}_wallet={wallet.get('wallet_key')} wallet_id={wallet.get('wallet_id')} "
                f"wallet_enabled={wallet_enabled} effective_enabled={effective_wallet_enabled} "
                f"credential_prefix={prefix} credential_status={credential_status} "
                f"live_orders_enabled={permissions.get('live_orders_enabled', False)}"
            )
            if check_env and enabled:
                _print_env_status(prefix, legacy_prefixes, role)
    print(f"enabled_account_group_count={enabled_count}")


def _plan_account_group_refs(plan: dict[str, Any]) -> list[dict[str, Any]]:
    refs = [item for item in plan.get("account_groups", ()) if isinstance(item, dict)]
    if refs:
        return refs

    legacy_refs: list[dict[str, Any]] = []
    for item in plan.get("wallet_groups", ()):
        if not isinstance(item, dict):
            continue
        wallet_group_id = str(item.get("wallet_group_id", ""))
        account_group_key = wallet_group_id.replace("_wallet_", "_") if "_wallet_" in wallet_group_id else wallet_group_id
        legacy_refs.append(
            {
                "account_group_key": account_group_key,
                "enabled": bool(item.get("enabled", False)),
            }
        )
    return legacy_refs


def _credential_status(prefix: str, legacy_prefixes: list[str]) -> str:
    primary = hibachi_credential_env(prefix)
    if _credential_env_complete(primary):
        return "usable"
    for legacy_prefix in legacy_prefixes:
        legacy = hibachi_credential_env(legacy_prefix)
        if _credential_env_complete(legacy):
            return f"usable_via_legacy:{legacy.prefix}"
    return "missing"


def _credential_env_complete(candidate: object) -> bool:
    return all(masked_env_status(name) != "missing" for name in candidate.required_names)


def _print_env_status(prefix: str, legacy_prefixes: list[str], role: str) -> None:
    candidates = [("primary", hibachi_credential_env(prefix))]
    candidates.extend(("legacy", hibachi_credential_env(item)) for item in legacy_prefixes)
    for label, names in candidates:
        print(f"  {role}_{label}_credential_prefix={names.prefix}")
        for name in names.required_names:
            print(f"  {role}_{label}_env_{name}={masked_env_status(name)}")


def _print_fx_phase(
    exchange_id: str,
    plan: dict[str, Any],
    market_by_symbol: dict[tuple[str, str], dict[str, Any]],
) -> None:
    fx = _dict(plan.get("fx_multiplier"))
    print(f"fx_multiplier_enabled={bool(fx.get('enabled', False))}")
    print(f"fx_volume_accounting={fx.get('volume_accounting', 'phase_total')}")
    print(f"fx_target_multiplier={float(fx.get('target_multiplier', 0)):.2f}x")
    print(f"fx_target_weekly_volume_usd={float(fx.get('target_weekly_volume_usd', 0)):.2f}")
    if fx.get("test_target_note"):
        print(f"fx_test_target_note={fx.get('test_target_note')}")
    for tier in fx.get("tiers", ()):
        if not isinstance(tier, dict):
            continue
        print(
            "fx_tier "
            f"volume_usd_per_market={float(tier.get('weekly_fx_volume_usd_per_market', 0)):.2f} "
            f"multiplier={float(tier.get('multiplier', 0)):.2f}x"
        )
    for market in fx.get("markets", ()):
        if not isinstance(market, dict):
            continue
        symbol = str(market.get("market", ""))
        catalog = market_by_symbol.get((exchange_id, symbol))
        catalog_status = "ok" if catalog else "missing"
        market_type = catalog.get("market_type") if catalog else "missing"
        multiplier_eligible = catalog.get("fx_multiplier_eligible") if catalog else False
        print(
            "fx_market "
            f"market={symbol} enabled={bool(market.get('enabled', False))} "
            f"spread_threshold_source={market.get('spread_threshold_source', 'unspecified')} "
            f"fallback_average_spread_bps={_format_optional_float(_spread_average_config(market))} "
            f"max_allowed_spread_bps={_format_optional_float(market.get('max_allowed_spread_bps'))} "
            f"market_order_safety_max_notional_usd="
            f"{_format_optional_float(market.get('market_order_safety_max_notional_usd'))} "
            f"market_order_liquidity_safety_fraction="
            f"{_format_optional_float(market.get('market_order_liquidity_safety_fraction'))} "
            f"catalog_status={catalog_status} market_type={market_type} "
            f"fx_multiplier_eligible={multiplier_eligible}"
        )


def _print_crypto_phase(
    exchange_id: str,
    plan: dict[str, Any],
    market_by_symbol: dict[tuple[str, str], dict[str, Any]],
) -> None:
    crypto = _dict(plan.get("crypto_volume"))
    print(f"crypto_volume_enabled={bool(crypto.get('enabled', False))}")
    print(f"crypto_target_weekly_volume_usd={float(crypto.get('target_weekly_volume_usd', 0)):.2f}")
    print(f"crypto_starts_after={crypto.get('starts_after', 'manual')}")
    for market in crypto.get("markets", ()):
        if not isinstance(market, dict):
            continue
        symbol = str(market.get("market", ""))
        catalog = market_by_symbol.get((exchange_id, symbol))
        catalog_status = "ok" if catalog else "missing"
        market_type = catalog.get("market_type") if catalog else "missing"
        print(
            "crypto_market "
            f"market={symbol} enabled={bool(market.get('enabled', False))} "
            f"spread_threshold_source={market.get('spread_threshold_source', 'unspecified')} "
            f"fallback_average_spread_bps={_format_optional_float(_spread_average_config(market))} "
            f"max_allowed_spread_bps={_format_optional_float(market.get('max_allowed_spread_bps'))} "
            f"market_order_safety_max_notional_usd="
            f"{_format_optional_float(market.get('market_order_safety_max_notional_usd'))} "
            f"market_order_liquidity_safety_fraction="
            f"{_format_optional_float(market.get('market_order_liquidity_safety_fraction'))} "
            f"catalog_status={catalog_status} market_type={market_type}"
        )


def _print_execution_sizing(plan: dict[str, Any]) -> None:
    sizing = _dict(plan.get("execution_sizing"))
    if not sizing:
        print("execution_sizing_status=missing")
        return
    print(f"execution_sizing_rule={sizing.get('order_sizing_rule', 'unspecified')}")
    print(f"max_order_notional_usd={float(sizing.get('max_order_notional_usd', 0)):.2f}")
    print(
        "market_order_safety_max_notional_usd="
        f"{float(sizing.get('market_order_safety_max_notional_usd', sizing.get('max_order_notional_usd', 0))):.2f}"
    )
    print(f"level_size_fraction={float(sizing.get('level_size_fraction', 0)):.4f}")
    print(
        "market_order_liquidity_safety_fraction="
        f"{float(sizing.get('market_order_liquidity_safety_fraction', 1)):.4f}"
    )
    print(f"gross_volume_counting={sizing.get('gross_volume_counting', 'unspecified')}")


def _format_optional_float(value: object) -> str:
    if value is None:
        return "none"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _spread_average_config(market: dict[str, Any]) -> object:
    return market.get("fallback_average_spread_bps", market.get("fallback_max_spread_bps"))


def _print_public_inventory_check(exchange_id: str, plan: dict[str, Any]) -> None:
    if exchange_id != "hibachi":
        print("public_inventory_check_skipped=non_hibachi_exchange")
        return

    from hibachi_xyz import HibachiApiClient

    client = HibachiApiClient()
    inventory = client.get_inventory()
    listed_symbols = {
        getattr(getattr(market, "contract", None), "symbol", "")
        for market in getattr(inventory, "markets", ())
    }
    wanted = _enabled_plan_markets(plan)
    print(f"public_inventory_symbol_count={len(listed_symbols)}")
    for symbol in wanted:
        print(f"public_inventory_market={symbol} listed={symbol in listed_symbols}")


def _enabled_plan_markets(plan: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    for phase_name in ("fx_multiplier", "crypto_volume"):
        phase = _dict(plan.get(phase_name))
        for market in phase.get("markets", ()):
            if isinstance(market, dict) and market.get("enabled", False):
                symbols.append(str(market.get("market", "")))
    return symbols


if __name__ == "__main__":
    main()
