# RiseX Phase 0 Onboarding

This repo stays inside:

```text
C:\Users\USER\Documents\PerpDEX-Farming-Bot
```

Do not use `PerpDEX-Farming-Bot 2` or any copied workspace.

## Official Docs Checked

- Main API docs: https://docs.risechain.com/docs/risex/api
- Full API reference: https://developer.rise.trade/reference/general-information
- Register signer: https://docs.risechain.com/docs/risex/api/register-signer
- Permit params: https://docs.risechain.com/docs/risex/api/creating-permit-params
- Place order: https://docs.risechain.com/docs/risex/api/examples/place-order
- Cancel order: https://docs.risechain.com/docs/risex/api/examples/cancel-order
- Update leverage: https://docs.risechain.com/docs/risex/api/examples/update-leverage
- Current place-order reference: https://developer.rise.trade/reference/orderservice_placeorder.md
- Current cancel-order reference: https://developer.rise.trade/reference/orderservice_cancelorder.md
- Current update-leverage reference: https://developer.rise.trade/reference/accountservice_updateleverage.md
- Order rate limits: https://docs.risechain.com/docs/risex/api/order-rate-limits
- Important notes: https://docs.risechain.com/docs/risex/api/important-notes

## Confirmed Shape

- REST public market data exists:
  - `GET /v1/markets`
  - `GET /v1/orderbook?market_id=...`
  - testnet REST: `https://api.testnet.rise.trade`
  - production REST: `https://api.rise.trade`
- WebSocket public orderbook data exists:
  - testnet: `wss://ws.testnet.rise.trade`
  - channel: `orderbook`
- Private read-only account checks can start with:
  - `GET /v1/account/balance?account=...`
  - `GET /v1/positions?account=...`
  - `GET /v1/nonce-state/{account}`
- Signer/authenticated order flow uses EIP-712 and a session signer.
- Signers are granted `Permission.All` by default when registered. The API key screen may not show a separate Perps toggle.
- The `docs.risechain.com` examples are useful for concepts, but do not match the production order endpoint shape used on 2026-07-06.
- Production place order currently uses the flat v3 body from `developer.rise.trade`: `market_id`, `size_steps`, `price_ticks`, `time_in_force`, `permit`, and `no_retry`.
- Production place order signs `VerifyWitness` with `account`, `target`, `hash`, `nonceAnchor`, `nonceBitmap`, and `deadline`; target is the router address from `/v1/system/config`.
- The successful production tiny BTC roundtrip used base64 EIP-2098 compact signatures, `client_order_id=0`, `price_ticks=0` for market orders, and bitmap nonce fields.
- `risex-client` exists, but docs describe it as an unofficial community TypeScript SDK. This Python repo should use direct REST/WebSocket skeletons until a Python SDK or a deliberate TypeScript helper boundary is chosen.
- The local preflight calculates integer size steps and price ticks for market safety checks. Market orders send `price_ticks=0`.

## Local `.env` Slots

Use local `.env` or OS environment variables only. Never put real keys in `.env.example`, docs, Obsidian, or chat.

```text
RISEX_API_ENDPOINT_PRODUCTION=https://api.rise.trade
RISEX_WSS_ENDPOINT_PRODUCTION=wss://ws.risex.trade
RISEX_ACCOUNT_ADDRESS_PRODUCTION=
RISEX_SIGNER_ADDRESS_PRODUCTION=
RISEX_SIGNER_PRIVATE_KEY_PRODUCTION=
```

The env checker prints only `present`/`missing`, parse status, and whether the signer address matches the private key. It never prints the key or the derived address.

## Safe Commands

Local env check, no network:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.check_risex_env --environment testnet
python -m perpdex_farming_bot.cli.check_risex_env --environment production
```

Prepared read-only smoke, no network:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment testnet
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment production
```

Public read-only network smoke:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment testnet --network --public --public-check markets
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment testnet --network --public --public-check orderbook --market-id 1
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment production --network --public --public-check markets
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment production --network --public --public-check orderbook --market-id 1
```

Private read-only network smoke requires only the account address in local `.env`:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment testnet --network --private-readonly --private-check positions
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment testnet --network --private-readonly --private-check nonce-state
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment production --network --private-readonly --private-check positions
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment production --network --private-readonly --private-check nonce-state
```

Live-test preflight is still read-only. It checks maintenance mode, fresh orderbook, market size/price steps, nonce state, positions, and signer key readiness. It does not submit or cancel orders.

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.risex_live_preflight --environment testnet --network --market-id 1 --max-notional-usd 10
python -m perpdex_farming_bot.cli.risex_live_preflight --environment production --network --market-id 1 --max-notional-usd 10
```

## Fee Provider

RiseX fee logic lives in:

```text
src/perpdex_farming_bot/exchanges/risex_fees.py
```

The live-volume CLI does not calculate fee directly. It asks
`RisexFeeProvider` for a `MarketFee`, then sends that into the shared execution
cost core:

```text
src/perpdex_farming_bot/core/execution_cost.py
```

Current RiseX account fee lookup uses private read-only
`GET /v1/trade-history`. It looks at recent taker fills and infers taker fee bps
from:

```text
abs(fee) / abs(price * size) * 10000
```

This is account-specific and uses only read-only data, but it is still an
inferred fee source rather than a direct fee-tier endpoint. If RiseX later
exposes a clear account fee endpoint, that should replace the inference path.

Fee source behavior:

1. read-only account fee from recent trade history, when available
2. valid config `fee_multiplier` applied to that account taker fee, when present
3. market metadata fee fields, if RiseX exposes them in market/account metadata
4. exact config override `entry_fee_bps` / `exit_fee_bps`
5. `fee_unknown` block

For market buy then reduce-only market sell, use taker fee for both entry and
exit. Maker fee is allowed only for a future strategy where the actual order
path guarantees maker execution.

Market fee settings are non-secret and live in:

```text
config/risex.live-volume.json
```

Example:

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
after checking the real fee. `fee_multiplier` is for temporary discounts. If
`fee_multiplier` is not `1`, `fee_multiplier_expires_at` is required. Missing
or expired expiry makes the multiplier ignored, and the printed source explains
why, for example:

```text
account_taker_fee_config_multiplier_missing_expiry_ignored
account_taker_fee_config_multiplier_expired_ignored
```

The RiseX volume selector now sorts by:

```text
expected_loss_bps =
  live_spread_bps
  + entry_fee_bps
  + exit_fee_bps
  + slippage_buffer_bps
```

If fee is unknown, the CLI prints `fee_unknown` and rejects that market. It
does not assume `0 bps`.

Dry-run emits a common event line for future GUI, Telegram, or local ledger
readers:

```text
execution_event_json={...}
```

The event uses labels rather than raw account values. It includes
`exchange`, `account_label`, `market`, `cycle_id`, `environment`, maker/taker
fee bps, entry/exit fee bps, `fee_source`, `fee_multiplier`,
`fee_multiplier_expires_at`, `live_spread_bps`, `expected_loss_bps`,
planned/filled gross volume, estimated fee/loss, start/final open-order counts,
start/final position counts, `error_reason`, and `status`.

Dry-run command, no live order:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.risex_live_volume --environment production --network --market-ids 1,2,4,5 --target-gross-volume-usd 20 --max-leg-notional-usd 10 --spread-bps 1 --book-fraction 0.5 --fast-close-on-fill --prebuild-close-order
```

## Fast Close Review

RiseX can support fast close because the current order path supports:

- market IOC entry with `price_ticks=0`
- reduce-only market IOC close
- session-signer EIP-712 signing
- nonce bitmap indexing, so the close can be pre-signed with the next bitmap
  index after the entry
- final read-only reconciliation through `GET /v1/positions` and
  `GET /v1/orders/open`

Fast close stays optional. Use `--fast-close-on-fill --prebuild-close-order`
only after the read-only checks and signing dry-run are clean. Without those
flags, the safer path waits for a position REST check before signing the close.

Remaining cautions:

- Partial entry fills can make a prebuilt close larger than the filled amount;
  reduce-only should prevent position flipping, but final reconciliation is
  still mandatory.
- Position REST can lag after an entry, so final position/open-order checks must
  always run.
- `client_order_id` uniqueness should be reviewed before larger or longer live
  loops.

Guarded production live test. This submits a real minimum BTC buy and then a reduce-only sell only when both `--execute-live` and the exact confirmation string are present.

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.risex_live_test --environment production --network --execute-live --confirm LIVE_RISEX_TINY_BTC_ROUNDTRIP --market-id 1 --max-notional-usd 10 --slippage-bps 25
```

The default signing mode is the production v3 path: `VerifyWitness` with a flat `permit` object and router target.

If RiseX returns `SignerNotAuthorized`, stop retrying live orders and verify signer authorization/permission or the current production request schema with RiseX before another live attempt.

Do not paste real account addresses, signer keys, signatures, session keys, or tokens into chat.

## Live Boundary

The RiseX adapter still blocks live order methods with `AdapterError`.

Before any live order path is implemented, confirm:

1. env check
2. public read-only
3. private read-only
4. paper-only
5. dry-run
6. explicit user approval for a tiny live test

Any future live path must re-check the latest orderbook/depth immediately before order submission and must keep order submission inside `src/perpdex_farming_bot/exchanges/risex.py`.
