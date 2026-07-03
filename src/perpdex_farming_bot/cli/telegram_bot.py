from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from perpdex_farming_bot.budget import current_weekly_window
from perpdex_farming_bot.config import BotConfig, load_config
from perpdex_farming_bot.cli.hibachi_weekly_status import _private_wallet_summary
from perpdex_farming_bot.cli.hibachi_weekly_live_test import (
    _build_runs,
    _dict,
    _execution_sizing,
    _load_json,
    _load_plan,
    _market_catalog,
    phase_target_totals,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present
from perpdex_farming_bot.runtime_control import (
    DEFAULT_RUNTIME_CONTROL_PATH,
    control_decision,
    format_control_state,
    load_runtime_control,
    set_enabled,
)
from perpdex_farming_bot.storage import WeeklyLedger


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram monitor/control bot for paper-only PerpDEX Farming Bot.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/hibachi.paper.json")
    parser.add_argument("--weekly-config", default="config/hibachi.weekly.test.json")
    parser.add_argument("--accounts-config", default="config/accounts.json")
    parser.add_argument("--markets-config", default="config/markets.json")
    parser.add_argument("--db", default=os.environ.get("PERPDEX_FARMING_BOT_DB", "data/hibachi_paper.sqlite"))
    parser.add_argument(
        "--live-ledger-db",
        default=os.environ.get("PERPDEX_HIBACHI_LIVE_LEDGER_DB", "data/hibachi_live_ledger.sqlite"),
    )
    parser.add_argument("--control-file", default=os.environ.get("PERPDEX_RUNTIME_CONTROL_FILE", DEFAULT_RUNTIME_CONTROL_PATH))
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--once", action="store_true", help="Process currently queued Telegram updates once, then exit.")
    args = parser.parse_args()

    load_dotenv_if_present(args.env_file)
    token = get_env("TELEGRAM_BOT_TOKEN")
    allowed_chat_id = get_env("TELEGRAM_CHAT_ID")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required in local .env")
    if not allowed_chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID is required in local .env")

    config = load_config(Path(args.config))
    offset = None
    print("telegram_bot=monitor_control")
    print("live_orders_enabled=False")
    print(
        "allowed_commands="
        "/help,/status,/status_hibachi,/balance,/volume,/settings,/wallets,"
        "/markets,/controls,/pause,/resume,/health,/live"
    )

    while True:
        updates = _telegram_get(token, "getUpdates", {"timeout": 10, "offset": offset})
        for update in updates.get("result", []):
            offset = int(update["update_id"]) + 1
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            text = str(message.get("text", "")).strip()
            if not _chat_allowed(chat, allowed_chat_id):
                continue
            if not text.startswith("/"):
                continue
            response = _handle_command(text, args, config)
            _telegram_get(token, "sendMessage", {"chat_id": str(chat.get("id", "")), "text": response[:3900]})

        if args.once:
            break
        time.sleep(max(0.0, args.poll_seconds))


def _handle_command(text: str, args: argparse.Namespace, config: BotConfig) -> str:
    parts = text.split()
    command = parts[0].split("@", 1)[0].lower()
    command_args = parts[1:]

    try:
        if command in {"/help", "/start"}:
            return _help_text()
        if command == "/status":
            if command_args:
                return _exchange_status_text(command_args[0], args, config)
            return _status_text(config, args)
        if command == "/settings":
            if command_args:
                return _exchange_status_text(command_args[0], args, config)
            return _settings_text(config)
        if command == "/balance":
            return _balance_text(config, args)
        if command == "/volume":
            return _volume_text(args)
        if command == "/health":
            return _health_text(args)
        if command == "/wallets":
            return _catalog_text(Path(args.accounts_config), "accounts")
        if command == "/markets":
            return _catalog_text(Path(args.markets_config), "markets")
        if command == "/controls":
            return format_control_state(load_runtime_control(args.control_file))
        if command in {"/pause", "/resume"}:
            return _set_control_text(args.control_file, command_args, enabled=command == "/resume")
        if command == "/live":
            return "live_orders_enabled=False\nlive control is blocked until a separate explicit approval step."
    except Exception as exc:
        return f"error={exc.__class__.__name__}: {exc}"

    return "unknown command. Send /help"


def _chat_allowed(chat: dict[str, object], allowed_chat_id: str) -> bool:
    allowed = allowed_chat_id.strip()
    if not allowed:
        return False
    chat_id = str(chat.get("id", ""))
    if allowed == chat_id:
        return True
    if allowed.startswith("@"):
        username = str(chat.get("username", ""))
        return username.casefold() == allowed[1:].casefold()
    return False


def _help_text() -> str:
    return "\n".join(
        (
            "PerpDEX Farming Bot commands:",
            "/status - bot/wallet/weekly allocation progress summary",
            "/status hibachi - Hibachi detailed weekly settings and strategy",
            "/balance - wallet balances and point field status, private read-only",
            "/volume - weekly volume progress by phase and market",
            "/settings - paper strategy/risk settings",
            "/wallets - configured account/wallet catalog",
            "/markets - configured market catalog",
            "/controls - current on/off controls",
            "/pause all | exchange hibachi | wallet hibachi_1_fx | market BTC/USDT-P",
            "/resume all | exchange hibachi | wallet hibachi_1_fx | market BTC/USDT-P",
            "/health - Telegram monitor health and local file paths",
            "/live - live-order gate status only; Telegram cannot start live orders",
        )
    )


def _status_text(config: BotConfig, args: argparse.Namespace) -> str:
    weekly = _weekly_context(args)
    state = load_runtime_control(args.control_file)
    enabled_groups, enabled_wallets = _enabled_account_counts(Path(args.accounts_config))
    targets = phase_target_totals(weekly["plan"], weekly["runs"])
    recorded = _phase_recorded_totals(weekly)
    total_recorded = weekly["ledger"].live_weekly_totals(weekly["week"]).planned_gross_volume_usd
    phase_count = sum(1 for label in ("fx", "crypto") if any(run.phase_label == label for run in weekly["runs"]))

    lines = [
        "status=monitoring",
        "running_bot=telegram_monitor_control",
        f"paper_mode={config.mode}",
        "live_orders_enabled=False",
        "live_order_start_from_telegram=False",
        f"exchange_count={len(config.exchanges)}",
        f"enabled_account_group_count={enabled_groups}",
        f"enabled_wallet_count={enabled_wallets}",
        f"configured_weekly_phase_count={phase_count}",
        f"monitored_market_count={len(weekly['runs'])}",
        f"period_start_utc={weekly['week'].start_utc.isoformat()}",
        f"period_end_utc={weekly['week'].end_utc.isoformat()}",
        f"fx_weekly_target_volume_usd={float(targets.fx_volume_usd):.2f}",
        f"fx_weekly_recorded_volume_usd={recorded['fx']:.2f}",
        f"fx_weekly_completion_pct={_pct(recorded['fx'], float(targets.fx_volume_usd))}",
        f"crypto_weekly_target_volume_usd={float(targets.crypto_volume_usd):.2f}",
        f"crypto_weekly_recorded_volume_usd={recorded['crypto']:.2f}",
        f"crypto_weekly_completion_pct={_pct(recorded['crypto'], float(targets.crypto_volume_usd))}",
        f"combined_weekly_target_volume_usd={float(targets.combined_volume_usd):.2f}",
        f"weekly_recorded_volume_usd={total_recorded:.2f}",
        f"weekly_completion_pct={_pct(total_recorded, float(targets.combined_volume_usd))}",
        "phases:",
    ]

    for run in weekly["runs"]:
        totals = weekly["ledger"].live_weekly_totals(
            weekly["week"],
            exchange_id=run.exchange_id,
            account_group_key=run.wallet.group_key,
            wallet_key=run.wallet.wallet_key,
            market=run.market,
        )
        decision = control_decision(
            state,
            exchange_id=run.exchange_id,
            wallet_id=run.wallet.wallet_key,
            market=run.market,
        )
        lines.append(
            f"- {run.phase_label} {run.market} wallet={run.wallet.wallet_key} "
            f"monitored=True recorded_market_volume_usd={totals.planned_gross_volume_usd:.2f} "
            f"control={decision.reason}"
        )

    lines.append("controls:")
    lines.append(format_control_state(state))
    lines.append("details=/status hibachi")
    return "\n".join(lines)


def _exchange_status_text(exchange_id: str, args: argparse.Namespace, config: BotConfig) -> str:
    exchange = exchange_id.strip().lower()
    if exchange != "hibachi":
        return f"exchange_status=unsupported\nexchange={exchange_id}\nconfigured_now=hibachi"

    weekly = _weekly_context(args)
    plan = weekly["plan"]
    sizing = _execution_sizing(plan)
    state = load_runtime_control(args.control_file)
    targets = phase_target_totals(plan, weekly["runs"])
    recorded = _phase_recorded_totals(weekly)
    total_recorded = weekly["ledger"].live_weekly_totals(weekly["week"], exchange_id="hibachi")

    week_config = _dict(plan.get("week"))
    safety = _dict(plan.get("safety"))
    fx = _dict(plan.get("fx_multiplier"))
    crypto = _dict(plan.get("crypto_volume"))

    lines = [
        "exchange_status=hibachi",
        f"weekly_config={args.weekly_config}",
        f"accounts_config={args.accounts_config}",
        f"markets_config={args.markets_config}",
        f"period_utc={weekly['week'].start_utc.isoformat()}..{weekly['week'].end_utc.isoformat()}",
        f"week_start_weekday_utc={week_config.get('start_weekday_utc', 'monday')}",
        f"live_orders_enabled_config={safety.get('live_orders_enabled', False)}",
        "telegram_live_order_control=False",
        "phase_sequence=fx_first_then_crypto",
        f"fx_weekly_target_or_cap_usd={float(targets.fx_volume_usd):.2f}",
        f"fx_weekly_recorded_volume_usd={recorded['fx']:.2f}",
        f"fx_weekly_completion_pct={_pct(recorded['fx'], float(targets.fx_volume_usd))}",
        f"crypto_weekly_target_or_cap_usd={float(targets.crypto_volume_usd):.2f}",
        f"crypto_weekly_recorded_volume_usd={recorded['crypto']:.2f}",
        f"crypto_weekly_completion_pct={_pct(recorded['crypto'], float(targets.crypto_volume_usd))}",
        f"combined_weekly_target_or_cap_usd={float(targets.combined_volume_usd):.2f}",
        f"combined_weekly_recorded_volume_usd={total_recorded.planned_gross_volume_usd:.2f}",
        f"combined_weekly_completion_pct={_pct(total_recorded.planned_gross_volume_usd, float(targets.combined_volume_usd))}",
        "execution_sizing:",
        f"- max_order_notional_usd={sizing.max_order_notional_usd}",
        f"- market_order_safety_max_notional_usd={sizing.market_order_safety_max_notional_usd}",
        f"- level_size_fraction={sizing.level_size_fraction}",
        f"- market_order_liquidity_safety_fraction={sizing.market_order_liquidity_safety_fraction}",
        f"- min_entry_delay_seconds={safety.get('min_entry_delay_seconds', 'config_missing')}",
        f"- max_idle_cycles={safety.get('max_idle_cycles', 'config_missing')}",
        "strategy:",
        "- current=paired_market_batch_market_buy_sell",
        "- spread_gate=current<=average_spread_bps AND current<=max_allowed_spread_bps",
        f"- fx_enabled={fx.get('enabled', False)} target_multiplier={fx.get('target_multiplier', 'n/a')}",
        f"- crypto_enabled={crypto.get('enabled', False)} starts_after={crypto.get('starts_after', 'n/a')}",
        "phase_details:",
    ]

    for run in weekly["runs"]:
        totals = weekly["ledger"].live_weekly_totals(
            weekly["week"],
            exchange_id=run.exchange_id,
            account_group_key=run.wallet.group_key,
            wallet_key=run.wallet.wallet_key,
            market=run.market,
        )
        decision = control_decision(
            state,
            exchange_id=run.exchange_id,
            wallet_id=run.wallet.wallet_key,
            market=run.market,
        )
        lines.append(
            f"- phase={run.phase_label} market={run.market} wallet={run.wallet.wallet_key} "
            f"monitored=True recorded_market_volume_usd={totals.planned_gross_volume_usd:.2f} "
            f"average_spread_bps={run.average_spread_bps:.4f} "
            f"max_allowed_spread_bps={run.max_allowed_spread_bps:.4f} "
            f"orderbook_granularity={run.orderbook_granularity} "
            f"control={decision.reason}"
        )

    tiers = fx.get("tiers", [])
    if tiers:
        lines.append("fx_multiplier_tiers:")
        for tier in tiers:
            lines.append(
                f"- volume={float(tier.get('weekly_fx_volume_usd_per_market', 0)):.0f} "
                f"multiplier={tier.get('multiplier')}"
            )

    lines.append("paper_assignments:")
    for assignment in config.strategy_assignments:
        if assignment.exchange_id == "hibachi":
            lines.append(
                f"- enabled={assignment.enabled} market={assignment.market} "
                f"strategy={assignment.strategy} wallet={assignment.wallet_id}"
            )

    return "\n".join(lines)


def _settings_text(config: BotConfig) -> str:
    return "\n".join(
        (
            "settings:",
            f"level_size_fraction={config.strategy.level_size_fraction}",
            f"notional_cap_usd={config.strategy.notional_cap_usd:.2f}",
            f"max_order_notional_usd={config.risk.max_order_notional_usd:.2f}",
            f"max_round_volume_usd={config.budget.max_round_volume_usd:.2f}",
            f"max_period_volume_usd={config.budget.max_period_volume_usd:.2f}",
            f"max_period_loss_usd={config.budget.max_period_loss_usd:.2f}",
            f"period_start_weekday_utc={config.budget.period_start_weekday_utc}",
            f"max_spread_bps={config.strategy.max_spread_bps:.4f}",
            "data_hub_average_rule=minimum positive value among 1D and 7D averages",
        )
    )


def _balance_text(config: BotConfig, args: argparse.Namespace) -> str:
    now = datetime.now(timezone.utc)
    week = current_weekly_window(now, config.budget.period_start_weekday_utc)
    first_assignment = next((item for item in config.strategy_assignments if item.enabled), None)
    if first_assignment is None:
        return "balance_status=no_enabled_assignment"

    ledger = WeeklyLedger(args.db)
    ledger.init()
    totals = ledger.weekly_totals(
        week,
        first_assignment.exchange_id,
        first_assignment.account_id,
        first_assignment.wallet_id,
    )
    live_weekly = _weekly_context(args)
    live_ledger = live_weekly["ledger"]
    live_ledger.init()
    live_totals = live_ledger.live_weekly_totals(live_weekly["week"])
    live_rows = live_ledger.live_weekly_market_totals(live_weekly["week"])
    private_status = _private_balance_summary(args.accounts_config)
    live_lines = [
        f"live_weekly_planned_volume_usd={live_totals.planned_gross_volume_usd:.2f}",
        f"live_weekly_recorded_round_count={live_totals.run_count}",
    ]
    for row in live_rows:
        live_lines.append(
            f"live_market account_group={row.account_group_key} wallet={row.wallet_key} "
            f"market={row.market} volume_usd={row.planned_gross_volume_usd:.2f} rounds={row.run_count}"
        )
    return "\n".join(
        (
            "balance=private_readonly",
            private_status,
            *live_lines,
            f"paper_weekly_volume_usd={totals.gross_volume_usd:.2f}",
            f"paper_weekly_realized_loss_usd={totals.realized_loss_usd:.6f}",
            f"paper_weekly_run_count={totals.run_count}",
            "points_note=shown_per_wallet_if_hibachi_sdk_exposes_a_points_field",
        )
    )


def _volume_text(args: argparse.Namespace) -> str:
    weekly = _weekly_context(args)
    targets = phase_target_totals(weekly["plan"], weekly["runs"])
    recorded = _phase_recorded_totals(weekly)
    total = weekly["ledger"].live_weekly_totals(weekly["week"])
    lines = [
        "volume=weekly_live_ledger",
        f"period_start_utc={weekly['week'].start_utc.isoformat()}",
        f"period_end_utc={weekly['week'].end_utc.isoformat()}",
        f"fx_target_volume_usd={float(targets.fx_volume_usd):.2f}",
        f"fx_recorded_volume_usd={recorded['fx']:.2f}",
        f"fx_completion_pct={_pct(recorded['fx'], float(targets.fx_volume_usd))}",
        f"crypto_target_volume_usd={float(targets.crypto_volume_usd):.2f}",
        f"crypto_recorded_volume_usd={recorded['crypto']:.2f}",
        f"crypto_completion_pct={_pct(recorded['crypto'], float(targets.crypto_volume_usd))}",
        f"combined_target_volume_usd={float(targets.combined_volume_usd):.2f}",
        f"recorded_volume_usd={total.planned_gross_volume_usd:.2f}",
        f"combined_completion_pct={_pct(total.planned_gross_volume_usd, float(targets.combined_volume_usd))}",
        f"recorded_round_count={total.run_count}",
        "markets:",
    ]
    for run in weekly["runs"]:
        run_total = weekly["ledger"].live_weekly_totals(
            weekly["week"],
            exchange_id=run.exchange_id,
            account_group_key=run.wallet.group_key,
            wallet_key=run.wallet.wallet_key,
            market=run.market,
        )
        lines.append(
            f"- phase={run.phase_label} market={run.market} wallet={run.wallet.wallet_key} "
            f"monitored=True recorded_market_volume_usd={run_total.planned_gross_volume_usd:.2f} "
            f"rounds={run_total.run_count}"
        )
    lines.append("source=local_bot_ledger_successful_confirmed_rounds")
    return "\n".join(lines)


def _health_text(args: argparse.Namespace) -> str:
    token_status = "present" if get_env("TELEGRAM_BOT_TOKEN") else "missing"
    chat_status = "present" if get_env("TELEGRAM_CHAT_ID") else "missing"
    control_state = load_runtime_control(args.control_file)
    return "\n".join(
        (
            "health=ok",
            f"timestamp_utc={datetime.now(timezone.utc).isoformat()}",
            "telegram_polling=running_if_this_message_was_received",
            f"telegram_token={token_status}",
            f"telegram_chat_id={chat_status}",
            f"paper_config={args.config}",
            f"weekly_config={args.weekly_config}",
            f"live_ledger_db={args.live_ledger_db}",
            f"paper_db={args.db}",
            f"control_file={args.control_file}",
            format_control_state(control_state),
        )
    )


def _weekly_context(args: argparse.Namespace) -> dict[str, object]:
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
    return {
        "plan": plan,
        "accounts": accounts,
        "markets": markets,
        "runs": runs,
        "week": week,
        "ledger": ledger,
    }


def _enabled_account_counts(accounts_path: Path) -> tuple[int, int]:
    raw = json.loads(accounts_path.read_text(encoding="utf-8"))
    enabled_groups = 0
    enabled_wallets = 0
    for group in raw.get("account_groups", ()):
        if not isinstance(group, dict) or not bool(group.get("enabled", False)):
            continue
        enabled_groups += 1
        wallets = group.get("wallets", {})
        if not isinstance(wallets, dict):
            continue
        for wallet in wallets.values():
            if isinstance(wallet, dict) and bool(wallet.get("enabled", False)):
                enabled_wallets += 1
    return enabled_groups, enabled_wallets


def _phase_recorded_totals(weekly: dict[str, object]) -> dict[str, float]:
    totals = {"fx": 0.0, "crypto": 0.0}
    for run in weekly["runs"]:
        run_total = weekly["ledger"].live_weekly_totals(
            weekly["week"],
            exchange_id=run.exchange_id,
            account_group_key=run.wallet.group_key,
            wallet_key=run.wallet.wallet_key,
            market=run.market,
        )
        totals[run.phase_label] = totals.get(run.phase_label, 0.0) + run_total.planned_gross_volume_usd
    return totals


def _pct(value: float, total: float) -> str:
    if total <= 0:
        return "n/a"
    return f"{(value / total) * 100:.2f}%"


def _private_balance_summary(accounts_config: str) -> str:
    prefixes = _account_credential_prefixes(Path(accounts_config))
    if not prefixes:
        prefixes = ["HIBACHI_1_CRYPTO"]
    return "\n".join(_private_wallet_summary(prefix) for prefix in prefixes)


def _account_credential_prefixes(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    prefixes: list[str] = []
    for group in raw.get("account_groups", ()):
        if not isinstance(group, dict) or not bool(group.get("enabled", False)):
            continue
        wallets = group.get("wallets", {})
        if not isinstance(wallets, dict):
            continue
        for role in ("fx", "crypto"):
            wallet = wallets.get(role, {})
            if not isinstance(wallet, dict) or not bool(wallet.get("enabled", False)):
                continue
            prefix = str(wallet.get("credential_env_prefix", ""))
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)
    return prefixes


def _catalog_text(path: Path, key: str) -> str:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get(key, [])
    if key == "accounts" and "account_groups" in raw:
        account_groups = raw.get("account_groups", [])
        lines = [f"account_groups_count={len(account_groups)}"]
        for group in account_groups:
            wallets = group.get("wallets", {})
            lines.append(
                f"- account_group={group.get('account_group_key')} enabled={group.get('enabled')} "
                f"exchange={group.get('exchange_id')}"
            )
            for role in ("crypto", "fx"):
                wallet = wallets.get(role, {})
                lines.append(
                    f"  - {role} wallet={wallet.get('wallet_key')} wallet_id={wallet.get('wallet_id')} "
                    f"prefix={wallet.get('credential_env_prefix')} live_orders={wallet.get('permissions', {}).get('live_orders_enabled')}"
                )
        return "\n".join(lines)

    lines = [f"{key}_count={len(items)}"]
    if key == "accounts":
        wallet_groups = raw.get("wallet_groups", [])
        lines.append(f"wallet_groups_count={len(wallet_groups)}")
        for group in wallet_groups:
            lines.append(
                f"- wallet_group={group.get('wallet_group_id')} enabled={group.get('enabled')} "
                f"crypto={group.get('crypto_account_key')} fx={group.get('fx_account_key')}"
            )
    for item in items:
        if key == "accounts":
            lines.append(
                f"- account={item.get('account_key')} type={item.get('account_type')} "
                f"exchange={item.get('exchange_id')} wallet={item.get('wallet_id')} "
                f"live_orders={item.get('permissions', {}).get('live_orders_enabled')}"
            )
        else:
            lines.append(
                f"- {item.get('market')} exchange={item.get('exchange_id')} "
                f"paper={item.get('enabled_for_paper')} live_orders={item.get('enabled_for_live_orders')}"
            )
    return "\n".join(lines)


def _set_control_text(control_file: str, args: list[str], *, enabled: bool) -> str:
    if not args:
        raise ValueError("usage: /pause all OR /pause exchange hibachi OR /pause wallet hibachi_wallet_1")
    scope = args[0]
    key = "all" if scope in {"all", "global"} else " ".join(args[1:])
    state = set_enabled(control_file, scope, key, enabled)
    action = "resumed" if enabled else "paused"
    return f"{action} {scope} {key}\n{format_control_state(state)}"


def _telegram_get(token: str, method: str, params: dict[str, object]) -> dict[str, object]:
    query = urlencode(params)
    request = Request(f"https://api.telegram.org/bot{token}/{method}?{query}", method="GET")
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
