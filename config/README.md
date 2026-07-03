# Config Files

This folder stores non-secret bot settings only.

Never put real API keys, private keys, signatures, session keys, or seed phrases in this folder.
Real credentials belong only in the local `.env` file or OS environment variables.

## Current Runtime Config

`hibachi.paper.json` is the active Hibachi paper-cycle config.

The command below reads this file by default:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle
```

Use this file for values that affect the current paper cycle:

- enabled markets
- per-order notional cap
- max round volume
- max weekly volume
- max weekly loss
- weekly start day
- max spread bps

## Account Catalog

`accounts.json` is an account catalog.

It stores:

- account labels
- exchange id
- wallet id
- env variable names
- permission flags
- Hibachi account groups that contain one Crypto wallet/API key set and one FX wallet/API key set

It must not store real key values.

For Hibachi, one logical account group can have two separated wallet/API key sets:

- Crypto prefix, for example `HIBACHI_1_CRYPTO`
- FX prefix, for example `HIBACHI_1_FX`

Existing local `HIBACHI_1_*` values are treated as a legacy alias for `HIBACHI_1_CRYPTO_*` during migration.

## Market Catalog

`markets.json` is a market catalog.

It stores:

- market symbols
- Data Hub symbols
- spread references
- paper-only threshold notes
- FX multiplier eligibility metadata

The CLI can use this file as the spread reference source:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle `
  --data-source market-config `
  --orderbook-source hibachi-sdk `
  --network `
  --no-record
```

That command reads the average spread from `markets.json`, reads the live public orderbook from Hibachi, and still sends no real orders.

`hibachi.paper.json` still controls runtime budgets such as max order notional, max weekly volume, weekly start day, and kill switch.

## Hibachi Weekly FX Multiplier Plan

`hibachi.weekly.json` is the non-secret production orchestration skeleton for the Hibachi-specific weekly flow:

1. Use the FX wallet/API key set to trade enabled FX market(s) until the weekly FX multiplier target is reached.
2. Then use the Crypto wallet/API key set to trade BTC volume under the configured spread rules.

The week is configured as UTC Monday 00:00 through next Monday 00:00.

Production targets:

- FX: `$1,000,000` per enabled FX market.
- Crypto BTC: `$1,000,000`.
- Order sizing: `min($100, market_order_safety_max_notional_usd, smaller best bid/ask level size * 0.5 * market_order_liquidity_safety_fraction)`.
- Default market-order safety cap: `$100`.
- Market-specific overrides can lower the safety cap or liquidity fraction later if a market needs a more conservative mode.

`hibachi.weekly.test.json` is the matching test plan:

- FX: `$10,000` on enabled FX market(s).
- Crypto BTC: `$10,000`.
- Order sizing: `min($100, market_order_safety_max_notional_usd, smaller best bid/ask level size * 0.5 * market_order_liquidity_safety_fraction)`.

Read-only plan check:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_weekly_plan --check-env
```

Test-plan check:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_weekly_plan `
  --weekly-config config/hibachi.weekly.test.json `
  --check-env
```

Optional public inventory symbol check:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_weekly_plan --check-env --network
```

See `docs/hibachi-weekly-fx-multiplier.md` for the beginner-friendly operational explanation.

## BTC Paper Monitor

The BTC monitor loop is paper-only and sends no real orders.

It uses:

- `config/hibachi.paper.json` for runtime budgets.
- `config/markets.json` for local spread references when `--data-source market-config` is used.
- Dashboard/Data Hub SQLite for the smaller positive 1D/7D spread average when `--data-source data-hub-window-min` is used.
- Hibachi public SDK orderbook for live best bid/ask depth when `--orderbook-source hibachi-sdk --network` is used.

Local reference smoke:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_monitor `
  --data-source market-config `
  --orderbook-source hibachi-sdk `
  --network `
  --no-record `
  --max-cycles 1 `
  --poll-seconds 0
```

Dashboard/Data Hub paper monitor:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_monitor `
  --data-source data-hub-window-min `
  --data-hub-db "C:\Users\redmi\Documents\PerpDEX-Dashboard-Data-Hub\data\live-5m-server.sqlite" `
  --orderbook-source hibachi-sdk `
  --network
```

This stops when recorded weekly paper gross volume reaches `max_period_volume_usd` / `--target-volume-usd`, currently `$1000`.

## One Live Roundtrip Test

Live order execution requires both `--execute-live` and the exact confirmation string.

The first-test live CLI defaults to `paired-market-batch`:

- It calculates one BTC quantity from the smaller best bid/ask level size and the notional cap.
- It re-checks the current Hibachi orderbook spread before every live batch.
- It submits equal market BUY and market SELL orders in one Hibachi batch request.
- It immediately reads the account position after the batch.
- If any residual long/short position remains, it sends a ReduceOnly market close for only that residual quantity.
- It waits at least 1 second before the next live entry batch, then checks spread again.

This reduces one-direction exposure compared with a hard-coded BUY-first test. It is still not risk-free: taker fees, slippage, rejected legs, partial fills, and exchange-side behavior can still create loss.

The older `buy-then-close` mode is still available and supports two close modes:

- `verified-position`: market buy, read the account position, then ReduceOnly market sell the detected long quantity.
- `immediate-reduce-only`: market buy, then immediately ReduceOnly market sell the same planned BTC quantity without a position read.

`immediate-reduce-only` reduces the delay before closing, while `ReduceOnly` prevents the close order from opening a new short if the buy did not open a long position.

Preflight only:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_live_roundtrip `
  --data-source market-config `
  --network `
  --max-notional-usd 5
```

Older BUY-first preflight:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_live_roundtrip `
  --data-source market-config `
  --network `
  --max-notional-usd 5 `
  --entry-mode buy-then-close `
  --close-mode verified-position
```

Actual live execution still requires:

```powershell
--execute-live --confirm LIVE_HIBACHI_BTC_MARKET_ROUNDTRIP
```

Limited live loop example:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_live_roundtrip `
  --data-source market-config `
  --network `
  --max-notional-usd 5 `
  --target-gross-volume-usd 50 `
  --poll-seconds 1 `
  --min-entry-delay-seconds 1 `
  --max-cycles 10 `
  --execute-live `
  --confirm LIVE_HIBACHI_BTC_MARKET_ROUNDTRIP
```

## Telegram Monitor

Telegram control is monitor/paper-control only. It does not enable live orders.

Required local `.env` values:

```text
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
PERPDEX_RUNTIME_CONTROL_FILE=data/runtime_control.json
```

Run:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.telegram_bot
```

Commands:

- `/balance`: private read-only balance, paper weekly volume, paper loss, run count, points status.
- `/status`: configured exchange, account, wallet, market, strategy, and control state.
- `/settings`: order sizing, weekly budget, spread rule, and risk settings.
- `/wallets`: configured wallet/account catalog.
- `/markets`: configured market catalog.
- `/controls`: current on/off state.
- `/pause all`, `/resume all`: pause/resume paper monitor globally.
- `/pause exchange hibachi`, `/resume exchange hibachi`: pause/resume one exchange.
- `/pause wallet hibachi_wallet_1`, `/resume wallet hibachi_wallet_1`: pause/resume one wallet.
- `/pause market BTC/USDT-P`, `/resume market BTC/USDT-P`: pause/resume one market.
- `/live`: shows that live-order control is blocked until a separate explicit approval step.

## Generic Demo Config

`default.paper.json` is the generic mock/demo strategy config.

Use it for local strategy skeleton tests that do not depend on Hibachi-specific settings.
