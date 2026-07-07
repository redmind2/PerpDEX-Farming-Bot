# Exchange Adapter And Local GUI Plan

This repo remains the active PerpDEX Farming Bot repo:

```text
C:\Users\USER\Documents\PerpDEX-Farming-Bot
```

## Boundaries

- Real secrets stay only in local `.env` or OS environment variables.
- `.env` is for secrets and secret-adjacent credential identifiers only.
- Non-secret settings should move from repo JSON files into local SQLite settings storage over time.
- Local web GUI is the primary operator surface.
- Telegram remains a secondary remote for status and pause/resume controls.
- Live order start remains blocked behind explicit CLI confirmation strings.

## Adapter Shape

Core code should call a common exchange adapter interface:

```text
core runner -> ExchangeAdapter -> exchange SDK/API
```

Current adapter files:

- `src/perpdex_farming_bot/exchanges/base.py`
- `src/perpdex_farming_bot/exchanges/hotstuff.py`
- `src/perpdex_farming_bot/exchanges/hibachi.py`
- `src/perpdex_farming_bot/exchanges/risex.py`
- `src/perpdex_farming_bot/exchanges/pacifica.py`
- `src/perpdex_farming_bot/exchanges/hyperliquid.py`
- `src/perpdex_farming_bot/exchanges/lighter.py`

The core runner lives at:

- `src/perpdex_farming_bot/core/live_volume.py`

The core runner does not import Hotstuff or Hibachi SDKs directly. Exchange-specific order submission belongs inside adapters.

## Execution Cost Core

Shared expected-loss and sizing math lives in:

- `src/perpdex_farming_bot/core/execution_cost.py`

Current shared formulas:

```text
expected_loss_bps =
  live_spread_bps
  + entry_fee_bps
  + exit_fee_bps
  + slippage_buffer_bps

per_side_cap_usd =
  min(order_notional_usd, remaining_gross_volume_usd / 2, smaller_top_level_notional_usd * level_size_fraction)
```

The sizing helper rounds amount down to the market `lot_size`, then checks the
planned side notionals against `min_order_size_usd`.

Fee source priority for every future adapter:

1. adapter/API automatic fee lookup
2. market/account metadata returned by the exchange
3. repo/local exact override, such as `entry_fee_bps` and `exit_fee_bps`
4. repo/local `fee_multiplier` with `fee_multiplier_expires_at`
5. if fee is still unknown, reject the trade candidate with `fee_unknown`

For market-order entry and close, the fee provider uses taker fee for both
entry and exit. Maker fee may be used only when the actual order path guarantees
maker execution. Fee values passed into the core are always bps: `0.0004` as an
exchange fee rate becomes `4 bps`.

`entry_fee_bps` and `exit_fee_bps` are exact absolute overrides. Use them only
after checking the real account or campaign fee. `fee_multiplier` is for a
temporary discount applied to the account fee, such as `0.5` for a 50% event
discount. If `fee_multiplier` is not `1`, `fee_multiplier_expires_at` is
required. Missing or expired expiry means the multiplier is ignored, and the
source string reports the reason.

Dashboard and Orderbook Forecast can provide signals only. They do not decide
fee truth and they never get order authority. Live orders should eventually pass
through a single Execution Gateway after this adapter layer is separated.

## Execution Event Fields

Dry-run and live CLIs should emit a secret-safe event line that GUI, Telegram,
or a future SQLite ledger can read later:

```text
execution_event_json={...}
```

The shared event shape lives in:

```text
src/perpdex_farming_bot/core/execution_event.py
```

The event uses labels, not raw wallet/account secrets. Current fields include:

```text
exchange
account_label
wallet_label
market
cycle_id
environment
fee_level
maker_fee_bps
taker_fee_bps
entry_fee_bps
exit_fee_bps
fee_source
fee_multiplier
fee_multiplier_expires_at
live_spread_bps
expected_loss_bps
planned_gross_volume_usd
filled_gross_volume_usd
estimated_fee_usd
estimated_loss_usd
realized_pnl_usd
points_estimate
start_position_count
final_position_count
start_open_order_count
final_open_order_count
order_ids
error_reason
status
```

This is not a live-order permission system. It is only a common record format
so future GUI/Telegram/SQLite code does not have to parse exchange-specific
console text.

## SDK Runtime

SDK and WebSocket dependencies are pinned in:

```text
requirements.txt
```

Setup notes for the development computer and future execution server are in:

```text
docs/runtime-setup.md
```

The execution server should install the same dependency file, but keep its own
server-local `.env`, `data/`, and `logs/`. Secrets do not move through Git.

## Hotstuff Env Model

Hotstuff uses an owner account wallet plus an API Wallet / agent signer:

```text
HOTSTUFF_ACCOUNT_ADDRESS_PRODUCTION=<owner wallet address>
HOTSTUFF_SIGNER_ADDRESS_PRODUCTION=<API Wallet address>
HOTSTUFF_SIGNER_PRIVATE_KEY_PRODUCTION=<API Wallet private key>
```

`HOTSTUFF_PRIVATE_KEY_PRODUCTION` remains a temporary legacy fallback only.

## Hotstuff Fee Provider

Hotstuff uses a dedicated fee provider in:

```text
src/perpdex_farming_bot/exchanges/hotstuff_fees.py
```

The account fee lookup calls Hotstuff `POST /info` method `user_fees` with the
account address as the read-only `user` param. The response fields used for
perp round trips are:

```text
perp_maker_fee_rate
perp_taker_fee_rate
total_volume_threshold
```

The CLI prints only safe fee numbers such as `account_maker_fee_bps` and
`account_taker_fee_bps`; it must not print account addresses or signatures.

Hotstuff dry-run/live-test selection now uses:

```text
expected_loss_bps =
  live_spread_bps
  + entry_fee_bps
  + exit_fee_bps
  + slippage_buffer_bps
```

Selection order is lowest `expected_loss_bps`, then lowest live spread, then
market name. If a market has no complete fee source, it is rejected with
`fee_unknown` and is never treated as zero-fee.

## SQLite Settings DB

Local non-secret settings DB:

```text
data/bot_settings.sqlite
```

Safe commands:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.settings_db init
python -m perpdex_farming_bot.cli.settings_db import-json --path config/hotstuff.live-test.json --namespace hotstuff.live_test
python -m perpdex_farming_bot.cli.settings_db list --namespace hotstuff.live_test
```

The DB is ignored by git through `data/` and `*.sqlite`.

## Local GUI

Run:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.gui
```

Open:

```text
http://127.0.0.1:8765
```

The GUI can:

- show secret presence as `present` or `missing`
- show runtime control state
- pause/resume all, exchange, wallet, or market scopes
- initialize the local settings DB

The GUI cannot:

- print secret values
- start live orders
- cancel orders
- open or close positions

Live actions still require a separate CLI command with exact confirmation text.

## RiseX Phase 0

RiseX is added as a separated Phase 0 skeleton:

- env check: `src/perpdex_farming_bot/cli/check_risex_env.py`
- REST read-only smoke: `src/perpdex_farming_bot/cli/risex_readonly_smoke.py`
- REST connector: `src/perpdex_farming_bot/connectors/risex_readonly.py`
- market data skeleton: `src/perpdex_farming_bot/marketdata/risex.py`
- adapter skeleton: `src/perpdex_farming_bot/exchanges/risex.py`

The current RiseX adapter can list positions through read-only REST, but live
order methods intentionally raise `AdapterError`. WebSocket orderbook data is
for opportunity monitoring; REST `/v1/orderbook` is the fresh verification path
before any future order submission.

## RiseX Fee Provider

RiseX now has a dedicated fee provider in:

```text
src/perpdex_farming_bot/exchanges/risex_fees.py
```

RiseX fee lookup is conservative. A current official account fee-tier endpoint
has not been wired in this repo, so the provider first tries a private
read-only `GET /v1/trade-history` lookup and infers recent taker fee bps from:

```text
abs(fee) / abs(price * size) * 10000
```

Only safe numbers are printed: `account_fee_level`, `account_maker_fee_bps`,
and `account_taker_fee_bps`. Account addresses, signer keys, signatures, and
session data must not be printed.

RiseX fee source behavior:

```text
read-only trade-history inferred taker fee, with valid config fee_multiplier if present
-> market metadata fee fields if the API exposes them
-> exact config override entry_fee_bps/exit_fee_bps
-> fee_unknown block
```

For market-order entry and close, RiseX uses taker fee for both entry and exit.
If a future maker-only RiseX strategy is added, maker fee may be used only when
the order path guarantees maker execution.

RiseX market config lives in:

```text
config/risex.live-volume.json
```

Market entries may include:

```json
{
  "market_id": 1,
  "entry_fee_bps": "4",
  "exit_fee_bps": "4",
  "fee_multiplier": "0.5",
  "fee_multiplier_expires_at": "2026-07-14T00:00:00+00:00",
  "slippage_buffer_bps": "0.5"
}
```

`entry_fee_bps` and `exit_fee_bps` are exact absolute overrides. Use them only
for a checked known fee. `fee_multiplier` represents a temporary discount on
the account taker fee, such as `0.5` for a 50% discount. If
`fee_multiplier != 1`, `fee_multiplier_expires_at` is required; missing or
expired expiry makes the provider ignore the multiplier and report the reason
in the source string, for example
`account_taker_fee_config_multiplier_missing_expiry_ignored` or
`account_taker_fee_config_multiplier_expired_ignored`.

The RiseX volume selector uses:

```text
expected_loss_bps =
  live_spread_bps
  + entry_fee_bps
  + exit_fee_bps
  + slippage_buffer_bps
```

Eligible markets are sorted by lowest `expected_loss_bps`, then lowest live
spread. If no complete fee source is available, the candidate is rejected with
`fee_unknown`; fee is never assumed to be zero.

RiseX dry-run now emits the shared event line:

```text
execution_event_json={...}
```

The event includes secret-safe fields for GUI/Telegram/ledger readers:
`exchange`, `account_label`, `market`, `cycle_id`, `environment`, fee bps,
`fee_source`, `fee_multiplier`, `fee_multiplier_expires_at`,
`live_spread_bps`, `expected_loss_bps`, planned and filled gross volume,
estimated fee/loss, start/final open-order counts, start/final position counts,
and `status`.

## RiseX Fast Close Notes

RiseX supports the fast-close model used by the guarded volume CLI:

- entry: market IOC buy
- close: reduce-only market IOC sell
- nonce: prebuilt by incrementing the signed entry nonce bitmap index
- client order body: flat v3 `VerifyWitness` request

Fast close remains optional. It is enabled only when
`--fast-close-on-fill --prebuild-close-order` are both present. The default live
boundary still requires read-only checks, local signing dry-run, `--execute-live`,
and the exact confirmation string. The safer non-fast-close path waits for a
position REST check before sizing the close.

Known fast-close cautions:

- A partially filled entry can make the prebuilt close amount too large. The
  current path relies on reduce-only protection and final reconciliation.
- REST position updates can lag, so final state must always check positions and
  open orders.
- `client_order_id` is currently a placeholder value in the flat request path;
  do not scale live loops until a per-order ID strategy is reviewed.

## Pacifica Phase 0

Pacifica is added as a separated Phase 0 plus guarded live-test skeleton:

- env check: `src/perpdex_farming_bot/cli/check_pacifica_env.py`
- REST read-only smoke: `src/perpdex_farming_bot/cli/pacifica_readonly_smoke.py`
- live-test preflight: `src/perpdex_farming_bot/cli/pacifica_live_preflight.py`
- guarded dry-run/live-test CLI: `src/perpdex_farming_bot/cli/pacifica_live_test.py`
- guarded live volume CLI: `src/perpdex_farming_bot/cli/pacifica_live_volume.py`
- configured spread table: `config/pacifica.live-volume.json`
- REST connector: `src/perpdex_farming_bot/connectors/pacifica_readonly.py`
- signed order connector: `src/perpdex_farming_bot/connectors/pacifica_trading.py`
- market data skeleton: `src/perpdex_farming_bot/marketdata/pacifica.py`
- adapter skeleton and guarded order submitter: `src/perpdex_farming_bot/exchanges/pacifica.py`

The Pacifica adapter can list positions through read-only REST and can submit a
pre-signed market order only when constructed with `allow_live_orders=True`.
The shared paired-roundtrip protocol methods still raise `AdapterError` until a
broader Pacifica volume-loop integration is reviewed. WebSocket BBO/orderbook
data is for opportunity monitoring; REST `/api/v1/book` is the fresh
verification path immediately before any live order submission.

The first Pacifica live test is capped by CLI at `--max-notional-usd <= 25`,
uses market buy then reduce-only market close, blocks when existing positions or
open orders are present, and requires:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.pacifica_live_test --environment testnet --network --symbol BTC --max-notional-usd 15 --execute-live --confirm LIVE_PACIFICA_TINY_MARKET_ROUNDTRIP
```

Do not run that command until env, public read-only, private read-only, and
dry-run output are clean.

The Pacifica volume CLI now uses the shared execution cost core to select the
lowest eligible expected-loss market from `config/pacifica.live-volume.json`.
It still keeps Pacifica-specific read-only checks, signing dry-run, confirmation
text, and live-order guardrails inside the Pacifica CLI/adapter files.

## Hibachi Fee Provider

Hibachi weekly live/dry-run selection now uses the shared execution cost core:

```text
expected_loss_bps =
  live_spread_bps
  + entry_fee_bps
  + exit_fee_bps
  + slippage_buffer_bps
```

Hibachi fee source priority:

```text
get_account_info tradeMakerFeeRate/tradeTakerFeeRate
-> get_inventory feeConfig tradeMakerFeeRate/tradeTakerFeeRate
-> exact config override entry_fee_bps/exit_fee_bps
-> valid config fee_multiplier with fee_multiplier_expires_at
-> fee_unknown block
```

For current Hibachi market-order round trips, the provider uses taker fee for
both entry and exit. Account fee lookup is read-only through the official
`hibachi-xyz` SDK `get_account_info`; metadata fallback is read-only through
`get_inventory().feeConfig`. The code prints only safe fee values such as
`maker_fee_bps`, `taker_fee_bps`, `fee_source`, and `expected_loss_bps`.

Market config may include:

```json
{
  "entry_fee_bps": 4,
  "exit_fee_bps": 4,
  "fee_multiplier": 0.5,
  "fee_multiplier_expires_at": "2026-07-14T00:00:00+00:00",
  "slippage_buffer_bps": 0.5
}
```

`entry_fee_bps` and `exit_fee_bps` are exact overrides. Account/metadata fees are
still queried first for safety logging, but a complete exact override replaces
the computed fee for that market and takes priority over `fee_multiplier`. If
`fee_multiplier != 1`, `fee_multiplier_expires_at` is required. Missing or
expired expiry causes the multiplier to be ignored, and the fee source string
explains why, for example
`account_taker_fee_config_multiplier_missing_expiry_ignored` or
`account_taker_fee_config_multiplier_expired_ignored`. If no complete fee source
is available, the market is rejected with `fee_unknown`; fee is never assumed to
be zero.

The Hibachi market selector now sorts eligible candidates by
`expected_loss_bps`, then by live spread. Spread thresholds still apply as a
separate safety gate.

## Pacifica Fee Provider

Pacifica now has a fee provider path:

```text
read Pacifica /account maker_fee/taker_fee -> exact config override if present -> account taker_fee * valid fee_multiplier -> account taker_fee -> fee_unknown block
```

For market-order round trips, Pacifica uses the account `taker_fee` for both
entry and exit. The REST `/api/v1/account` response returns `fee_level`,
`maker_fee`, and `taker_fee`; rates such as `0.0004` are converted to bps by
multiplying by `10000`, so `0.0004` becomes `4 bps`.

Use `entry_fee_bps` and `exit_fee_bps` only for a checked absolute fee override.
If an exact override is present it wins over `fee_multiplier`. Market-specific
campaigns can use `fee_multiplier`; for example, RWA 50% fee is represented as
`fee_multiplier = 0.5` plus `fee_multiplier_expires_at`. If the expiry is
missing or already passed, the multiplier is ignored and the full account taker
fee is used. The source string reports the reason, such as
`account_taker_fee_config_multiplier_missing_expiry_ignored` or
`account_taker_fee_config_multiplier_expired_ignored`. If no complete fee source
is available, the market is rejected with `fee_unknown`; it is never assumed to
be zero.

## Pacifica Ledger/Event Fields

`pacifica_live_volume` now emits machine-readable key-value fields beside the
existing human log lines. They are not a database yet; they are stable deltas for
a future Telegram/GUI console or local ledger writer to consume.

Every run announces:

```text
ledger_event_schema_version=1
ledger_event_output=key_value_lines
ledger_event_time_scopes=cycle,day_utc,iso_week_utc
```

Dry-run and live cycle outputs include `*_ledger_*` fields such as:

```text
dry_run_ledger_event_type=cycle_result
dry_run_ledger_exchange=pacifica
dry_run_ledger_run_mode=dry_run
dry_run_ledger_gross_volume_usd=...
dry_run_ledger_cycle_gross_volume_usd=...
dry_run_ledger_cycle_volume_usd_delta=...
dry_run_ledger_day_volume_usd_delta=...
dry_run_ledger_week_volume_usd_delta=...
dry_run_ledger_expected_loss_bps=...
dry_run_ledger_loss_status=expected
dry_run_ledger_loss_usd=...
dry_run_ledger_cycle_expected_loss_usd=...
dry_run_ledger_expected_fee_usd=...
dry_run_ledger_fee_event_status=active|none|missing_expiry_ignored|expired_ignored|exact_override
dry_run_ledger_points_status=not_available
dry_run_ledger_error_alert=false
```

The day/week fields are per-event deltas, not accumulated totals. Telegram/GUI
should sum them by `day_utc` or `iso_week_utc`. Points are explicitly marked
`not_available` until Pacifica exposes or the project defines a point formula.
Error conditions emit `*_ledger_event_type=error_alert` with
`*_ledger_error_alert=true` and a reason string. Error event volume/loss deltas
are zero so failed cycles are not counted into daily or weekly totals. If a
candidate existed, `*_ledger_planned_candidate_gross_volume_usd` is reference
only. Final live checks emit `final_ledger_event_type=run_final_state` with
open-order and position counts.

## Telegram Role

Telegram remains a secondary remote:

- status
- balance
- volume
- pause/resume
- health

Telegram must not start live trading directly.

## Hyperliquid Adapter

Hyperliquid is added as a separated read-only, dry-run preflight, guarded tiny
BTC live-test path, and guarded spread-volume path through the shared
live-volume core:

- env check: `src/perpdex_farming_bot/cli/check_hyperliquid_env.py`
- read-only smoke: `src/perpdex_farming_bot/cli/hyperliquid_readonly_smoke.py`
- live preflight with no orders: `src/perpdex_farming_bot/cli/hyperliquid_live_preflight.py`
- guarded tiny BTC live-test CLI: `src/perpdex_farming_bot/cli/hyperliquid_live_test.py`
- guarded spread-gated volume CLI: `src/perpdex_farming_bot/cli/hyperliquid_live_volume_test.py`
- configured market draft: `config/hyperliquid.live-volume.json`
- configured spread-volume test: `config/hyperliquid.spread-volume-test.json`
- REST `/info` connector: `src/perpdex_farming_bot/connectors/hyperliquid_readonly.py`
- unsigned close-request preview helper: `src/perpdex_farming_bot/connectors/hyperliquid_trading.py`
- market data skeleton: `src/perpdex_farming_bot/marketdata/hyperliquid.py`
- adapter and guarded order submitter: `src/perpdex_farming_bot/exchanges/hyperliquid.py`
- fee provider: `src/perpdex_farming_bot/exchanges/hyperliquid_fees.py`

The Hyperliquid adapter can list positions through private read-only
`clearinghouseState`, check open orders, verify signer env presence without
printing secrets, and execute a paired IOC roundtrip only when
`allow_live_orders=True`. Entry orders are normal IOC orders. Close orders are
reduce-only IOC orders sized from the actual entry fill.

`hyperliquid_live_test` remains the one capped BTC path. The measured-volume
path now flows through the common runner:

```text
hyperliquid_live_volume_test -> core.live_volume.run_paired_volume -> HyperliquidAdapter
```

`hyperliquid_live_volume_test` checks current spread against
`min(1 bps, configured average spread)`, sizes each leg by top-of-book 50% or
`$100`, closes immediately reduce-only through the adapter, waits one second
through the common core delay, then rescans.

For Hyperliquid HIP-3 commodity/equity-style markets, config must distinguish
display names from API coin names. For example `SAMSUNG-PERP` maps to
`api_coin=xyz:SMSN` with `dex=xyz`, and `SKHYNICS-PERP` maps to
`api_coin=xyz:SKHX` with `dex=xyz`; `WTIOIL-PERP` maps to `api_coin=xyz:CL`
with `dex=xyz`. The live-volume CLI initializes the SDK with every configured
perp dex before any dry-run or live check.

Hyperliquid fee priority is:

```text
official SDK or /info userFees
-> market metadata if usable fee fields appear
-> exact config override
-> fee_unknown block
```

For current market-style entry and close planning, the provider uses taker fee
for both entry and exit. It records maker fee from `userFees`, but maker fee may
be used only by a future order path that guarantees maker execution.

Hyperliquid market precision comes from `meta`:

```text
size lot = 10 ^ -szDecimals
perp price decimals <= 6 - szDecimals, plus the 5 significant figures rule
```

`config/hyperliquid.live-volume.json` sets `min_order_size_usd` to `10` for the
first mainnet draft, matching the official exchange endpoint example error that
orders below `$10` are rejected. If Hyperliquid changes this rule, update local
config before enabling any future live path.

Fast close prebuild remains a review-only option. `--fast-close-on-fill
--prebuild-close-order` reports the required close path, but no pre-signed close
request is produced. The guarded live paths sign the reduce-only close only
after the entry fill and use the actual filled size, followed by open-order and
final-position reconciliation.

## Lighter Phase 0

Lighter is added as a separated Phase 0 read-only skeleton:

- env check: `src/perpdex_farming_bot/cli/check_lighter_env.py`
- read-only smoke: `src/perpdex_farming_bot/cli/lighter_readonly_smoke.py`
- REST connector: `src/perpdex_farming_bot/connectors/lighter_readonly.py`
- market data helper: `src/perpdex_farming_bot/marketdata/lighter.py`
- adapter skeleton: `src/perpdex_farming_bot/exchanges/lighter.py`
- fee provider skeleton: `src/perpdex_farming_bot/exchanges/lighter_fees.py`
- onboarding notes: `docs/lighter-onboarding.md`

The Lighter adapter can read account snapshots/positions through
`GET /api/v1/account` and can read open orders only when a read-only auth token
is present. Live order, cancel, transfer, withdrawal, and reduce-only close
paths intentionally raise `AdapterError` in Phase 0.

Official docs confirm production REST at:

```text
https://mainnet.zklighter.elliot.ai
```

They also confirm WebSocket endpoints:

```text
wss://mainnet.zklighter.elliot.ai/stream
wss://testnet.zklighter.elliot.ai/stream
```

The testnet REST base URL is intentionally not defaulted until an official
source confirms it.

Lighter fee priority is:

```text
orderBooks market metadata taker fee percentage
-> exact config override
-> fee_unknown block
```

For future market-style entry and close planning, the provider uses taker fee
for both entry and exit. The fee is never assumed to be zero.
