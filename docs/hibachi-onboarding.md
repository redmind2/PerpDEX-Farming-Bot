# Hibachi Onboarding Plan

## Short Answer

Yes, connect your Hibachi API key, but do it one step at a time.

The first test should not place orders. The first test should only prove:

- the key exists locally
- the code can read the key without printing it
- the environment is ready for a later read-only API smoke test

## Why This Order Is Safer

For a trading bot, API setup has two different risks:

1. Secret leak risk: accidentally putting a private key in code, config, git, or chat.
2. Trading risk: accidentally calling an order endpoint too early.

So the safe order is:

```text
1. Local env check, no network
2. Public API check, no key
3. Private read-only account check, key required
4. Paper strategy using Hibachi market data
5. Dry-run order intent, no real order
6. Real order only after explicit approval
```

## Current Scope For This Repo

This repo is now scoped to same-exchange farming strategies.

For Hibachi, that means:

- one Hibachi exchange connector
- one or more Hibachi accounts/wallets
- Hibachi markets only
- no cross-exchange arbitrage
- no exchange-to-exchange transfer/rebalance logic

Cross-exchange logic belongs in `Cross-Exchange-Trading-Bot`.

## Local Secret Setup

Copy `.env.example` to `.env` locally, then fill only the Hibachi variables.

Do not paste real keys into chat.

Required local variables:

```text
HIBACHI_API_KEY_PRODUCTION=
HIBACHI_PUBLIC_KEY_PRODUCTION=
HIBACHI_PRIVATE_KEY_PRODUCTION=
HIBACHI_ACCOUNT_ID_PRODUCTION=
```

Use the unnumbered `HIBACHI_...` variables only when you run commands without `--account-id`.
For the current `hibachi_1` flow, prefer the numbered variables below.

For multiple API keys/accounts, use numbered prefixes:

```text
HIBACHI_1_API_KEY_PRODUCTION=
HIBACHI_1_PUBLIC_KEY_PRODUCTION=
HIBACHI_1_PRIVATE_KEY_PRODUCTION=
HIBACHI_1_ACCOUNT_ID_PRODUCTION=

HIBACHI_2_API_KEY_PRODUCTION=
HIBACHI_2_PUBLIC_KEY_PRODUCTION=
HIBACHI_2_PRIVATE_KEY_PRODUCTION=
HIBACHI_2_ACCOUNT_ID_PRODUCTION=
```

Prefix matching is case-insensitive. `hibachi_1`, `HIBACHI_1`, and `Hibachi_1` all map to the same `HIBACHI_1_...` env names.

What each value means:

- `HIBACHI_1_API_KEY_PRODUCTION`: the Hibachi API key string.
- `HIBACHI_1_PUBLIC_KEY_PRODUCTION`: the account public key shown by Hibachi. The current REST private-read-only smoke does not pass it to the SDK, but websocket/trading flows may need it later.
- `HIBACHI_1_PRIVATE_KEY_PRODUCTION`: the private signing key or HMAC key provided for the Hibachi API. Keep it only in local `.env`.
- `HIBACHI_1_ACCOUNT_ID_PRODUCTION`: the numeric Hibachi account ID. The SDK expects this to be digits.

Repo config files:

- `config/accounts.json`: account metadata and env variable names only. Never put real key values there.
- `config/markets.json`: market metadata and paper-only thresholds, including the BTC 7d spread reference.
- `config/hibachi.paper.json`: the actual paper-cycle config currently used by the CLI.

Optional endpoint variables:

```text
HIBACHI_API_ENDPOINT_PRODUCTION=https://api.hibachi.xyz
HIBACHI_DATA_API_ENDPOINT_PRODUCTION=https://data-api.hibachi.xyz
```

The repo stores only variable names, not real values.

## First Local Check

After filling `.env`, run:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.check_hibachi_env
```

This command only checks whether required values exist. It does not call Hibachi and does not print the secret values.

The output should say `present` or `missing`. It should never print the real key, private key, signature, or session value.

For a specific account prefix:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.check_hibachi_env --account-id hibachi_1
```

## Read-Only Smoke Harness

The next command prepares the read-only smoke test path without sending any network request by default:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_readonly_smoke
```

This command checks:

- local Hibachi env values are present or missing
- Hibachi base URLs are HTTPS
- order, cancel, transfer, and position-changing actions are disabled
- private network calls are still skipped until auth/SDK details are confirmed

After the official Hibachi docs confirm the exact public market metadata or orderbook path, run a public-only GET test like this:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_readonly_smoke --public-path "/CONFIRMED_PUBLIC_READONLY_PATH" --network
```

You can also store comma-separated public read-only paths in local `.env`:

```text
HIBACHI_PUBLIC_READONLY_PATHS_PRODUCTION=/CONFIRMED_PUBLIC_READONLY_PATH
```

Do not put private account IDs, signatures, session tokens, or key values in that path variable. Keep private read-only checks as a separate later step once the official SDK/auth flow is confirmed.

## Hibachi Paper Cycle For The First Strategy

After env checks are safe, the first Hibachi-specific strategy work should still be paper-only:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle
```

This command does not place orders. It checks the configured Hibachi paper markets, creates paper market-market round-trip intents, sends those intents through the risk engine, simulates paper fills, and writes weekly metrics into:

```text
data/hibachi_paper.sqlite
```

The strategy rule is:

```text
current spread <= average spread threshold
and, if max_spread_bps > 0, current spread <= max_spread_bps
```

Then size is:

```text
min(smaller best bid/ask level size * 50%, configured max order notional)
```

Beginner version: the bot first asks, "Is the spread cheap enough?" If yes, it pretends to buy and immediately sell back in paper mode, then records the paper volume and paper loss.

## Official SDK Notes

Hibachi's official Python package is `hibachi-xyz`.

```powershell
python -m pip install hibachi-xyz
```

The package uses symbols like:

```text
BTC/USDT-P
SOL/USDT-P
```

Safe public SDK smoke:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_sdk_smoke --network --public --symbol BTC/USDT-P
```

If `--granularity` is omitted or set to `0`, the command chooses one of the symbol's allowed orderbook granularities from Hibachi inventory.

Safe private read-only SDK smoke:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_sdk_smoke --network --private-readonly
```

For a specific account prefix:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_sdk_smoke --account-id hibachi_1 --network --private-readonly
```

Allowed now:

- `get_exchange_info`
- `get_prices`
- `get_stats`
- `get_orderbook`
- `get_account_info`
- `get_capital_balance`

Still forbidden until explicit approval:

- `place_market_order`
- `place_limit_order`
- `cancel_order`
- `cancel_all_orders`
- `batch_orders`
- `withdraw`
- `transfer`

Weekly budget behavior:

- `period_name` should be `weekly` for the Hibachi paper config.
- `period_start_weekday_utc` chooses the UTC weekday that starts the week, for example `monday`.
- `max_period_volume_usd` is the weekly volume cap for the current account/wallet.
- `max_period_loss_usd` is the weekly loss cap.
- The SQLite ledger records weekly volume, paper realized PnL, paper realized loss, and placeholders for balance/points fields that can be filled after private read-only APIs are confirmed.

Multiple markets:

- Add or enable more Hibachi `strategy_assignments` in `config/hibachi.paper.json`.
- The paper cycle checks each enabled Hibachi market assignment once.
- The current command uses mock/dashboard-shaped snapshots. Live Hibachi market data and private read-only balance/points checks should be connected only after official endpoint/SDK details are confirmed.

Server Data Hub read-only mode:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle `
  --data-source data-hub `
  --orderbook-source hibachi-sdk `
  --network `
  --data-hub-db "C:\Users\redmi\Documents\PerpDEX-Dashboard-Data-Hub\data\live-5m-server.sqlite"
```

This reads only the Data Hub spread signal with `mode=ro`, then reads live public orderbook depth from Hibachi SDK. It does not write to the Data Hub DB and still does not send real orders.

Temporary spread cap test:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle --max-spread-bps 2.5 --no-record
```

## Later API Smoke Tests

After the local env check passes, the next tests should be:

1. Public market metadata.
2. Public orderbook/market data for the exact Hibachi market symbol.
3. Private read-only account info.
4. Private read-only balance/collateral info.
5. Paper strategy run using Hibachi data.

Only after those pass should we discuss any order endpoint.

## SDK Note

Hibachi publishes a Python SDK. The local Python version in this workspace is compatible with the SDK's Python version requirement, but installing or calling it may require network access and should be done as a separate step.
