# Hyperliquid Onboarding

This pass adds Hyperliquid as a separated adapter path for read-only checks,
dry-run preflight, one guarded tiny BTC live-test CLI, and the guarded
spread-volume loop through the common live-volume core. It does not enable
cancels, transfers, or broad position-changing requests outside those explicit
CLI-confirmed live paths.

## Sources

Use official Hyperliquid sources first:

- API docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
- Info endpoint: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- Exchange endpoint: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
- Tick and lot size: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size
- Nonces and API wallets: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets
- Signing: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/signing
- Official Python SDK: https://github.com/hyperliquid-dex/hyperliquid-python-sdk

CCXT, Dwellir, Hydromancer, and community SDKs are secondary references only.

## Safety Boundary

Current allowed steps:

1. local env check
2. public read-only `/info`
3. private read-only `/info`
4. dry-run preflight
5. guarded tiny BTC live test with exact CLI confirmation
6. guarded spread-volume test through `core.live_volume.run_paired_volume`
   with exact CLI confirmation

Current blocked steps:

- cancel submission
- transfer and withdraw requests
- general position-changing requests outside the guarded tiny BTC and
  spread-volume CLIs
- pre-signed fast close orders

The guarded live-test path is limited to a fresh BTC minimum-size entry and a
reduce-only close sized from the actual fill. Any broader live path still needs
a separate CLI confirmation, volume cap, kill switch, fresh orderbook check,
and final position/open-order reconciliation.

The guarded spread-volume CLI now builds a `RoundtripPlan` and hands execution
to the shared live-volume core:

```text
hyperliquid_live_volume_test -> core.live_volume.run_paired_volume -> HyperliquidAdapter
```

The adapter keeps exchange-specific live authority. It submits IOC entry
orders only when `allow_live_orders=True`, closes reduce-only using the actual
entry fill size, and refuses the roundtrip if existing open orders are detected.
The Hyperliquid live-volume configs set `allow_live_orders` to `true`; the CLI
still requires `--execute-live` plus the exact confirmation string before any
order is sent.

## Env Names

Keep real values only in local `.env` or OS environment variables. Do not put
real values in Git, docs, or chat.

```text
HYPERLIQUID_ACCOUNT_ADDRESS_PRODUCTION=
HYPERLIQUID_API_WALLET_ADDRESS_PRODUCTION=
HYPERLIQUID_API_WALLET_PRIVATE_KEY_PRODUCTION=
HYPERLIQUID_VAULT_ADDRESS_PRODUCTION=
HYPERLIQUID_API_ENDPOINT_PRODUCTION=https://api.hyperliquid.xyz

HYPERLIQUID_ACCOUNT_ADDRESS_TESTNET=
HYPERLIQUID_API_WALLET_ADDRESS_TESTNET=
HYPERLIQUID_API_WALLET_PRIVATE_KEY_TESTNET=
HYPERLIQUID_VAULT_ADDRESS_TESTNET=
HYPERLIQUID_API_ENDPOINT_TESTNET=https://api.hyperliquid-testnet.xyz
```

`ACCOUNT_ADDRESS` is the master account or subaccount address used for account
queries. `API_WALLET_*` is for signing only. Do not query account state with the
API wallet address.

`VAULT_ADDRESS` is optional and should be used only after the master signing plus
vault/subaccount behavior is reviewed against official docs and SDK code.

## Safe Commands

Local env check, no network:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.check_hyperliquid_env --environment production
```

Public read-only smoke:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hyperliquid_readonly_smoke --environment production --network --public --public-check meta
python -m perpdex_farming_bot.cli.hyperliquid_readonly_smoke --environment production --network --public --public-check l2-book --coin BTC
```

Private read-only smoke:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hyperliquid_readonly_smoke --environment production --network --private-readonly --private-check state
python -m perpdex_farming_bot.cli.hyperliquid_readonly_smoke --environment production --network --private-readonly --private-check user-fees
```

Dry-run preflight, still no orders:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hyperliquid_live_preflight --environment production --network --config config/hyperliquid.live-volume.json
```

Fast close review mode:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hyperliquid_live_preflight --environment production --network --fast-close-on-fill --prebuild-close-order
```

This only reports that close prebuild must wait for the actual entry fill size.
It does not sign or send a close order.

Guarded tiny BTC live test:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hyperliquid_live_test --network --coin BTC
python -m perpdex_farming_bot.cli.hyperliquid_live_test --network --coin BTC --execute-live --confirm LIVE_HYPERLIQUID_TINY_BTC_ROUNDTRIP
```

Do not run the second command until env, public read-only, private read-only,
and the dry-run output are clean. The command blocks when starting positions or
open orders are detected, caps one-side notional, uses IOC entry, uses
reduce-only IOC close, and checks final position/open-order state.

Guarded spread-gated volume test:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hyperliquid_live_volume_test --config config/hyperliquid.spread-volume-test.json --network
python -m perpdex_farming_bot.cli.hyperliquid_live_volume_test --config config/hyperliquid.spread-volume-test.json --network --execute-live --confirm LIVE_HYPERLIQUID_1000_SPREAD_VOLUME
```

The config uses the supplied 5m average spreads. A market is eligible only when
current spread is less than or equal to `min(1 bps, market average spread)`.
Each leg is capped by the smaller of top-of-book 50%, `$100`, and remaining
target sizing. The loop buys, immediately sends a reduce-only close for the
actual filled size through the Hyperliquid adapter, waits one second through
the shared core delay, then scans spreads again.

## Market Name Alignment

The spread table uses display names such as `SAMSUNG-PERP`, while Hyperliquid
HIP-3 markets use API coin names that include the perp dex prefix. Keep both
names in config:

```text
display_market -> api_coin / dex
BRENTOIL-PERP  -> xyz:BRENTOIL / xyz
EWY-PERP       -> xyz:EWY / xyz
GOLD-PERP      -> xyz:GOLD / xyz
SAMSUNG-PERP   -> xyz:SMSN / xyz
SILVER-PERP    -> xyz:SILVER / xyz
SKHYNICS-PERP  -> xyz:SKHX / xyz
WTIOIL-PERP    -> xyz:CL / xyz
```

Default perp markets such as `BTC`, `ETH`, `HYPE`, and `SOL` keep an empty dex.
Live-volume code uses `display_market` only for logs and `api_coin` for
Hyperliquid orderbook and order requests. When a supplied average spread is
`n/a`, the live-volume test uses the configured hard spread cap, currently
`1 bps`, as the threshold.

## Fee Provider

`HyperliquidFeeProvider` uses this priority:

1. `userFees` from the official SDK or direct Hyperliquid `/info`
2. market metadata, if a future response exposes usable fee fields
3. exact config override
4. block with `fee_unknown`

For current market-style entry and close planning, taker fee is used for both
entry and exit. Maker fee is recorded, but it must be used only by a future path
that guarantees maker execution.

`fee_multiplier` can model a market/account event discount. If it is not `1`,
`fee_multiplier_expires_at` is required. Missing or expired expiry makes the
provider ignore the multiplier and report that in the fee source.

## Market Metadata

The read-only and guarded live-test market data paths use:

- `meta` for coin name, asset id, and `szDecimals`
- `l2Book` for fresh bid/ask and top-level size
- `allMids` for public mids smoke testing

Size rounding uses `szDecimals` with `Decimal`. Price precision follows the
official rule: perps allow up to 5 significant figures and no more than
`6 - szDecimals` decimal places. The guarded live-test CLI applies this
precision before submitting IOC orders through the official Python SDK.

`min_order_size_usd` is set to `10` in the first mainnet config draft because
the official exchange endpoint examples include the error
`Order must have minimum value of $10.` If Hyperliquid changes this rule, update
the local config before any future live path is enabled.

## Fast Close Notes

Fast close pre-signing remains deferred. The adapter does perform a fast
post-fill reduce-only close in the guarded live paths, but it does not pre-sign
the close before the entry is filled. A safe future pre-signed fast-close path
must:

- wait for the entry order result
- calculate close size from actual filled size
- handle partial fill, no fill, already filled, canceled, and rejected states
- use reduce-only close orders
- consider `scheduleCancel` and `cancelByCloid`
- reconcile final position and open orders
- avoid stale nonce or `expiresAfter` assumptions

## Added Files

- `config/hyperliquid.live-volume.json`
- `config/hyperliquid.spread-volume-test.json`
- `src/perpdex_farming_bot/connectors/hyperliquid_readonly.py`
- `src/perpdex_farming_bot/connectors/hyperliquid_trading.py`
- `src/perpdex_farming_bot/exchanges/hyperliquid.py`
- `src/perpdex_farming_bot/exchanges/hyperliquid_fees.py`
- `src/perpdex_farming_bot/marketdata/hyperliquid.py`
- `src/perpdex_farming_bot/cli/check_hyperliquid_env.py`
- `src/perpdex_farming_bot/cli/hyperliquid_readonly_smoke.py`
- `src/perpdex_farming_bot/cli/hyperliquid_live_preflight.py`
- `src/perpdex_farming_bot/cli/hyperliquid_live_test.py`
- `src/perpdex_farming_bot/cli/hyperliquid_live_volume_test.py`
