# Hibachi Weekly FX Multiplier Plan

This document describes the Hibachi-specific weekly flow.

No real API keys, private keys, signatures, session keys, or seed phrases belong in this repo.
Real values stay only in local `.env` or OS environment variables.

## Why This Is Special

Hibachi separates FX and Crypto wallets/accounts:

- FX markets have a separate wallet, API keys, and balance.
- Crypto markets have a separate wallet, API keys, and balance.
- One logical Hibachi account group therefore needs two credential sets.

For `Hibachi 1`, the current non-secret prefix plan is:

- Crypto wallet/account: `HIBACHI_1_CRYPTO`
- FX wallet/account: `HIBACHI_1_FX`

That means local `.env` should contain these names, with real values filled locally only:

```text
HIBACHI_1_CRYPTO_API_KEY_PRODUCTION=
HIBACHI_1_CRYPTO_PUBLIC_KEY_PRODUCTION=
HIBACHI_1_CRYPTO_PRIVATE_KEY_PRODUCTION=
HIBACHI_1_CRYPTO_ACCOUNT_ID_PRODUCTION=

HIBACHI_1_FX_API_KEY_PRODUCTION=
HIBACHI_1_FX_PUBLIC_KEY_PRODUCTION=
HIBACHI_1_FX_PRIVATE_KEY_PRODUCTION=
HIBACHI_1_FX_ACCOUNT_ID_PRODUCTION=
```

Existing local `.env` files that already use `HIBACHI_1_*` are treated as a legacy alias for `HIBACHI_1_CRYPTO_*` during migration.

## Weekly Window

Hibachi weekly accounting is configured as:

- timezone: UTC
- week start: Monday 00:00 UTC
- week end: next Monday 00:00 UTC, exclusive

This is stored in:

```text
config/hibachi.weekly.json
```

The reduced live-test plan is stored separately:

```text
config/hibachi.weekly.test.json
```

## Phase Order

The planned weekly flow is:

1. FX phase
   - Use the FX wallet/account.
   - Trade enabled FX market(s).
   - FX has its own weekly target volume, separate from Crypto.
   - Production target is currently `$1,000,000` weekly FX volume.
   - Enabled FX markets are monitored as candidates; the phase stops when the FX target is reached.

2. Crypto phase
   - Use the Crypto wallet/account.
   - Trade BTC only at first.
   - Crypto has its own weekly target volume, separate from FX.
   - Production target is currently `$1,000,000` BTC weekly volume after the FX phase.
   - Enabled Crypto markets are monitored as candidates; the phase stops when the Crypto target is reached.
   - Use the smaller positive 1D/7D average spread bps from Dashboard/Data Hub when available.
   - Current test fallback threshold is `0.16 bps`, based on the provided 7d BTC average.

The config shape is:

```text
fx_multiplier.target_weekly_volume_usd     = total weekly FX target
crypto_volume.target_weekly_volume_usd     = total weekly Crypto target
markets[].enabled                          = whether that market is monitored as a candidate
markets[].fallback_average_spread_bps      = average spread fallback until Dashboard is connected
markets[].max_allowed_spread_bps           = hard spread cap; current spread must be below this too
```

The current test plan uses smaller targets:

```text
FX test volume:  $10,000
BTC test volume: $10,000
```

This test target does not unlock the first Hibachi FX multiplier tier. It exists to verify credential separation, balance separation, order sizing, spread checks, residual-position handling, and rate-limit-safe pacing before using larger weekly targets.

Both production and test plans use this order sizing rule:

```text
order size = min($100, market_order_safety_max_notional_usd, smaller best bid/ask level size * 50% * market_order_liquidity_safety_fraction)
```

The default safety cap is `$100`. Market-specific overrides can lower the cap or liquidity fraction later if a market needs a more conservative mode.

The FX multiplier tiers currently encoded from the provided screenshot are:

```text
$100,000   -> 1.05x
$200,000   -> 1.10x
$400,000   -> 1.20x
$600,000   -> 1.30x
$1,000,000 -> 1.50x
```

## Current FX Market Skeleton

Public Hibachi inventory showed these FX symbols:

```text
AUD/USDT-P
GBP/USDT-P
EUR/USDT-P
NZD/USDT-P
```

The initial weekly plan enables only:

```text
EUR/USDT-P
```

The other FX markets are present but disabled until deliberately enabled.

## Read-Only Plan Check

Use this before adding live FX execution:

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

Optional public market-symbol verification:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_weekly_plan --check-env --network
```

Both commands are read-only and do not place or cancel orders.

## Live Safety Boundary

Current files still default live order permission to false:

- `config/accounts.json`
- `config/markets.json`
- `config/hibachi.weekly.json`

Future live FX execution should require a separate explicit confirmation, just like the BTC live roundtrip CLI.
