# Pacifica Phase 0 Onboarding

This repo stays inside:

```text
C:\Users\USER\Documents\PerpDEX-Farming-Bot
```

Do not use `PerpDEX-Farming-Bot 2` or any copied workspace.

## Official Docs Checked

Checked on 2026-07-06:

- Main site: https://www.pacifica.fi/
- API overview: https://docs.pacifica.fi/api-documentation/api
- REST API: https://docs.pacifica.fi/api-documentation/api/rest-api
- WebSocket API: https://docs.pacifica.fi/api-documentation/api/websocket
- Market symbols: https://docs.pacifica.fi/api-documentation/api/market-symbols
- Tick and lot size: https://docs.pacifica.fi/api-documentation/api/tick-and-lot-size
- Market info: https://docs.pacifica.fi/api-documentation/api/rest-api/markets/get-market-info
- Orderbook: https://docs.pacifica.fi/api-documentation/api/rest-api/markets/get-orderbook
- Account info: https://docs.pacifica.fi/api-documentation/api/rest-api/account/get-account-info
- Positions: https://docs.pacifica.fi/api-documentation/api/rest-api/account/get-positions
- Create market order: https://docs.pacifica.fi/api-documentation/api/rest-api/orders/create-market-order
- Create limit order: https://docs.pacifica.fi/api-documentation/api/rest-api/orders/create-limit-order
- Get open orders: https://docs.pacifica.fi/api-documentation/api/rest-api/orders/get-open-orders
- Get order history by ID: https://docs.pacifica.fi/api-documentation/api/rest-api/orders/get-order-history-by-id
- Signing: https://docs.pacifica.fi/api-documentation/api/signing
- Signing implementation: https://docs.pacifica.fi/api-documentation/api/signing/implementation
- Signing operation types: https://docs.pacifica.fi/api-documentation/api/signing/operation-types
- API Agent Keys: https://docs.pacifica.fi/api-documentation/api/signing/api-agent-keys
- Rate limits: https://docs.pacifica.fi/api-documentation/api/rate-limits
- Last order ID: https://docs.pacifica.fi/api-documentation/api/last-order-id
- Delayed account positions FAQ: https://docs.pacifica.fi/api-documentation/api/api-faq/delayed-account_positions
- Python SDK examples: https://github.com/pacifica-fi/python-sdk

## Confirmed Shape

- REST public market data exists:
  - `GET /api/v1/info`
  - `GET /api/v1/book?symbol=...&agg_level=...`
  - `GET /api/v1/prices`
- WebSocket public market data exists:
  - mainnet: `wss://ws.pacifica.fi/ws`
  - testnet: `wss://test-ws.pacifica.fi/ws`
  - `bbo` stream for best bid/ask
  - `book` stream for aggregated orderbook updates
- Private read-only account checks can start with:
- `GET /api/v1/account?account=...`
  - returns safe fee fields `fee_level`, `maker_fee`, and `taker_fee`
- `GET /api/v1/positions?account=...`
  - `GET /api/v1/orders?account=...`
  - `GET /api/v1/orders/history_by_id?order_id=...`
- Market symbols are case-sensitive. For example, `BTC` is valid; `btc` is not the same symbol.
- Order prices and amounts must respect each market's `tick_size` and `lot_size` from `GET /api/v1/info`.
- Market orders use `POST /api/v1/orders/create_market` with signing operation type `create_market_order`.
- Limit orders use `POST /api/v1/orders/create` with signing operation type `create_order`.
- All POST requests require Ed25519 signatures. GET requests and WebSocket subscriptions do not require signatures.
- Pacifica supports API Agent Keys, also called Agent Wallets in the API docs. The Pacifica UI labels the public key as `API Key`; the REST request still sends it in the `agent_wallet` field.
- The API Agent private key signs on behalf of the account without exposing the original wallet private key to the bot.
- Signing uses recursively sorted JSON keys, compact JSON, UTF-8 bytes, Ed25519 signing, and base58-encoded signatures.
- Official Python SDK/example repo exists, but this repo keeps a small direct implementation instead of routing all exchange work through SDK examples.
- Pacifica snapshot account position/order channels can lag under load. The first CLI uses REST positions only as a conservative check; production-grade state should later use event-driven account trade/order-update streams.

## Phase 0 Files

- env check: `src/perpdex_farming_bot/cli/check_pacifica_env.py`
- REST read-only smoke: `src/perpdex_farming_bot/cli/pacifica_readonly_smoke.py`
- live-test preflight: `src/perpdex_farming_bot/cli/pacifica_live_preflight.py`
- guarded dry-run/live-test CLI: `src/perpdex_farming_bot/cli/pacifica_live_test.py`
- guarded live volume CLI: `src/perpdex_farming_bot/cli/pacifica_live_volume.py`
- live-test helper functions: `src/perpdex_farming_bot/cli/pacifica_live_common.py`
- REST connector: `src/perpdex_farming_bot/connectors/pacifica_readonly.py`
- signed order connector: `src/perpdex_farming_bot/connectors/pacifica_trading.py`
- market data skeleton: `src/perpdex_farming_bot/marketdata/pacifica.py`
- adapter skeleton and guarded signed order submitter: `src/perpdex_farming_bot/exchanges/pacifica.py`

The current Pacifica adapter can list positions through read-only REST and can
submit a pre-signed market order only when `allow_live_orders=True`. The shared
paired-roundtrip adapter protocol methods still raise `AdapterError` until the
broader Pacifica volume-loop integration is reviewed.

## Safe Commands

Local env check, no network:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.check_pacifica_env --environment testnet
```

Prepared read-only smoke, no network:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.pacifica_readonly_smoke --environment testnet
```

Public read-only network smoke:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.pacifica_readonly_smoke --environment testnet --network --public --public-check markets
python -m perpdex_farming_bot.cli.pacifica_readonly_smoke --environment testnet --network --public --public-check orderbook --symbol BTC --agg-level 1
```

Private read-only network smoke requires only the account address in local `.env`:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.pacifica_readonly_smoke --environment testnet --network --private-readonly --private-check positions
```

Live-test preflight with no network:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.pacifica_live_preflight --environment testnet
```

Live-test preflight with read-only network checks:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.pacifica_live_preflight --environment testnet --network --symbol BTC --max-notional-usd 15
```

Guarded dry-run. This signs a request shape when local agent credentials and
signing dependencies are present, but it does not submit orders without
`--execute-live` and the exact confirmation string:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.pacifica_live_test --environment testnet --network --symbol BTC --max-notional-usd 15
```

Guarded volume-loop dry-run using the configured spread table:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.pacifica_live_volume --environment production --network --config config/pacifica.live-volume.json
```

Do not paste real account addresses, API Agent private keys, signatures, session
keys, or tokens into chat.

## Live Boundary

Before running the guarded live command, confirm:

1. env check
2. public read-only
3. private read-only
4. paper-only
5. dry-run
6. explicit user approval for a tiny live test

The guarded live command is:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.pacifica_live_test --environment testnet --network --symbol BTC --max-notional-usd 15 --execute-live --confirm LIVE_PACIFICA_TINY_MARKET_ROUNDTRIP
```

Do not run it until read-only and dry-run output are clean. It re-checks the
latest Pacifica orderbook immediately before submission and routes the actual
POST through `src/perpdex_farming_bot/exchanges/pacifica.py`.

The guarded volume-loop live command is:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.pacifica_live_volume --environment production --network --config config/pacifica.live-volume.json --execute-live --confirm LIVE_PACIFICA_1000_VOLUME
```

The configured sizing rule is:

```text
min($100, remaining gross target / 2, smaller top-of-book level notional * 50%)
```

The current config counts planned entry notional plus planned reduce-only close
notional toward the target gross volume. Fee fields can be added per market as
`entry_fee_bps` and `exit_fee_bps`; the selection metric is
`live_spread_bps + entry_fee_bps + exit_fee_bps + slippage_buffer_bps`.

## Fee And Expected-Loss Core

Pacifica live volume selection uses the shared core file:

```text
src/perpdex_farming_bot/core/execution_cost.py
```

The Pacifica CLI still owns Pacifica-specific safety checks, read-only account
state checks, local signing dry-run, and explicit live confirmation. The shared
core only calculates expected loss and order sizing.

Fee source priority for Pacifica and future adapters:

1. adapter/API automatic fee lookup
2. market/account metadata from Pacifica `/account`, `/info`, or an equivalent exchange endpoint
3. exact config override values such as `entry_fee_bps` and `exit_fee_bps`
4. config `fee_multiplier` with `fee_multiplier_expires_at`
5. if fee is unknown, reject the market candidate as `fee_unknown`

Pacifica now has a provider path that reads account fee data with
`GET /api/v1/account?account=...`. The CLI only prints safe fee numbers:
`fee_level`, `maker_fee_bps`, and `taker_fee_bps`; it must not print account
addresses or keys. For market-order round trips, the CLI uses the account
`taker_fee` for both entry and exit unless a complete explicit config override
is set.

Pacifica returns fee rates as decimal rates. The provider converts them to bps:

```text
0.0004 * 10000 = 4 bps
0.00015 * 10000 = 1.5 bps
```

Current provider path:

```text
read Pacifica /account maker_fee/taker_fee -> exact config override if present -> account taker_fee * valid fee_multiplier -> account taker_fee -> fee_unknown block
```

If all automatic/config fee sources are missing, the market is rejected as
`fee_unknown`; the CLI should not silently assume fee is zero.

Config override example:

```json
{
  "symbol": "HYPE",
  "display_market": "HYPE-PERP",
  "current_spread_bps": "0.14",
  "spread_5m_bps": "0.14",
  "spread_1h_bps": "0.15",
  "spread_24h_bps": "0.29",
  "fee_multiplier": "1",
  "slippage_buffer_bps": "0"
}
```

Use `entry_fee_bps` and `exit_fee_bps` only when an exact absolute fee override
has been checked. If exact override and multiplier are both present, exact
override wins. Use `fee_multiplier` for market-specific discounts that should
follow the current account fee tier. Any multiplier other than `1` must include
`fee_multiplier_expires_at`; otherwise it is ignored and the full account taker
fee is used.

Ignored multiplier source strings:

```text
account_taker_fee_config_multiplier_missing_expiry_ignored
account_taker_fee_config_multiplier_expired_ignored
```

For example, RWA 50% fee uses:

```json
"fee_multiplier": "0.5",
"fee_multiplier_expires_at": "2026-07-14T00:00:00+00:00"
```

For the current Pacifica RWA 50% event, this represents "through July 13, 2026
UTC"; when the timestamp passes, the bot automatically stops applying the
discount. Update or remove the config entry only after re-checking the live
campaign.

Dashboard and Orderbook Forecast remain signal providers only; they do not get
order authority.

## Ledger/Event Output For Operations Console

`pacifica_live_volume` prints normal human-readable lines and also emits
machine-readable `*_ledger_*` lines for a future Telegram/GUI operations console.
No database write is added yet; these are safe dry-run/live result fields that a
later common ledger can consume.

Every run announces:

```text
ledger_event_schema_version=1
ledger_event_output=key_value_lines
ledger_event_time_scopes=cycle,day_utc,iso_week_utc
```

Successful dry-run and live cycles emit `*_ledger_event_type=cycle_result`.
Important fields:

```text
*_ledger_exchange=pacifica
*_ledger_run_mode=dry_run|live
*_ledger_status=...
*_ledger_market=...
*_ledger_symbol=...
*_ledger_gross_volume_usd=...
*_ledger_cycle_volume_usd_delta=...
*_ledger_day_volume_usd_delta=...
*_ledger_week_volume_usd_delta=...
*_ledger_expected_loss_bps=...
*_ledger_loss_status=expected
*_ledger_loss_usd=...
*_ledger_expected_fee_usd=...
*_ledger_fee_event_status=active|none|missing_expiry_ignored|expired_ignored|exact_override
*_ledger_fee_event_multiplier=...
*_ledger_fee_event_expires_at=...
*_ledger_points_status=not_available
*_ledger_error_alert=false
```

The day/week values are per-event deltas. Telegram/GUI should sum them by
`day_utc` and `iso_week_utc`. Points stay `not_available` until Pacifica exposes
an official points source or this project defines a checked formula.

Error paths emit `*_ledger_event_type=error_alert` and
`*_ledger_error_alert=true`. Error events set volume and loss deltas to zero, so
the console can alert without accidentally adding failed cycles to daily/weekly
totals. If a candidate existed, `*_ledger_planned_candidate_gross_volume_usd`
is printed only as reference.
