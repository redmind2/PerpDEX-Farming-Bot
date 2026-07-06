# Hotstuff Phase 0 Onboarding

Hotstuff integration starts with the same safety order used for Hibachi:

1. Local env check only
2. Public read-only
3. Private read-only
4. Paper-only
5. Dry-run only after approval
6. Live orders only after explicit approval

No real API key, private key, signature, session key, or Telegram token belongs in chat or repo files. Real values stay only in local `.env` or OS environment variables.

## Official References

- REST API introduction: https://docs.hotstuff.trade/api-reference/getting-started/introduction
- WebSocket introduction: https://docs.hotstuff.trade/wss/getting-started/introduction
- Account fee method: https://docs.hotstuff.trade/api-reference/info/accounts/user-fees
- Fee tiers: https://docs.hotstuff.trade/hotstuff-docs/trading/fees
- Python SDK: https://github.com/hotstuff-labs/python-sdk

The REST read-only skeleton uses `POST /info` with a JSON body containing `method` and `params`.

## Local Env Names

Use empty placeholders in `.env.example`, and put real values only in local `.env`.

```text
HOTSTUFF_API_ENDPOINT_PRODUCTION=https://api.hotstuff.trade
HOTSTUFF_API_ENDPOINT_TESTNET=https://testnet-api.hotstuff.trade
HOTSTUFF_WSS_ENDPOINT_PRODUCTION=wss://api.hotstuff.trade/ws
HOTSTUFF_WSS_ENDPOINT_TESTNET=wss://testnet-api.hotstuff.trade/ws

HOTSTUFF_ACCOUNT_ADDRESS_PRODUCTION=
HOTSTUFF_ACCOUNT_ADDRESS_TESTNET=

HOTSTUFF_SIGNER_ADDRESS_PRODUCTION=
HOTSTUFF_SIGNER_PRIVATE_KEY_PRODUCTION=
HOTSTUFF_SIGNER_ADDRESS_TESTNET=
HOTSTUFF_SIGNER_PRIVATE_KEY_TESTNET=

# Legacy fallback only. New setup should use HOTSTUFF_SIGNER_PRIVATE_KEY_*.
HOTSTUFF_PRIVATE_KEY_PRODUCTION=
HOTSTUFF_PRIVATE_KEY_TESTNET=
```

`HOTSTUFF_ACCOUNT_ADDRESS_*` is the owner/account wallet used for private read-only account data.
`HOTSTUFF_SIGNER_ADDRESS_*` and `HOTSTUFF_SIGNER_PRIVATE_KEY_*` are the Hotstuff API Wallet / agent signer used for programmatic trading.

For current private read-only checks, the bot sends only the account address as `user`. It does not send any private key to `/info`.

## Safe Commands

Local-only env check:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.check_hotstuff_env
```

Prepared smoke check with no network request:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hotstuff_readonly_smoke
```

Public read-only ticker check:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hotstuff_readonly_smoke --network --public --public-method ticker --symbol all
```

Public read-only orderbook check:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hotstuff_readonly_smoke --network --public --public-method orderbook --symbol BTC-PERP
```

Private read-only account summary check:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hotstuff_readonly_smoke --network --private-readonly --private-method account-summary
```

Mainnet live-test preflight, still with no orders:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hotstuff_live_preflight
```

Guarded live-test CLI dry run, still with no orders:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hotstuff_live_test
```

## Fee Provider

Hotstuff fee calculation is separated in:

```text
src/perpdex_farming_bot/exchanges/hotstuff_fees.py
```

The read-only account fee source is Hotstuff `POST /info` method `user_fees`.
The bot reads only safe fee fields:

```text
perp_maker_fee_rate
perp_taker_fee_rate
total_volume_threshold
```

`perp_taker_fee_rate` is used for both entry and exit in market-order round
trips. A rate such as `0.00025` is converted to `2.5 bps` before it is passed
to the shared execution-cost core.

Market config may include these non-secret fee fields:

```json
{
  "entry_fee_bps": "2.5",
  "exit_fee_bps": "2.5",
  "fee_multiplier": "0.5",
  "fee_multiplier_expires_at": "2026-07-14T00:00:00+00:00",
  "slippage_buffer_bps": "0"
}
```

Use `entry_fee_bps` and `exit_fee_bps` only for an exact checked override. If
both exact override fields are present, they win over a multiplier.
`fee_multiplier` is for temporary discounts applied to the account fee. If the
multiplier is not `1`, `fee_multiplier_expires_at` is required; missing or
expired expiry ignores the multiplier and prints a reason such as
`account_taker_fee_config_multiplier_missing_expiry_ignored` or
`account_taker_fee_config_multiplier_expired_ignored`.

Hotstuff market selection uses:

```text
expected_loss_bps =
  live_spread_bps
  + entry_fee_bps
  + exit_fee_bps
  + slippage_buffer_bps
```

The selected market is the eligible candidate with the lowest
`expected_loss_bps`, then lowest spread. If fee cannot be known from account
API, exchange metadata, or exact config, the market is rejected as `fee_unknown`;
the bot never assumes zero fee.

## Execution Events

Hotstuff preflight and guarded dry-run emit a secret-safe event line:

```text
execution_event_json={...}
```

This line is designed for a future GUI, Telegram status command, or SQLite
ledger. It includes labels and safe numeric fields only, such as market,
environment, maker/taker fee bps, entry/exit fee bps, fee source,
fee multiplier/expiry, live spread, expected loss, planned volume, estimated
fee/loss, position/order counts, order IDs if available, and status. It must
not include raw account addresses, private keys, signatures, or tokens.

## Fast Close Review

Hotstuff supports the pieces needed for guarded fast close:

- market and limit orders
- IOC time in force
- reduce-only orders through `ro=true`
- client order id field `cloid`
- read-only `positions`, `openOrders`, and `fills` checks

Current implementation keeps fast close behind options only:

```powershell
--fast-close-on-fill
--prebuild-close-order
```

The default path remains slower and more conservative. In fast-close mode, the
entry order is submitted first. If the entry is fully filled, the CLI can submit
a prebuilt reduce-only close immediately. If the entry is partially filled, the
prebuilt close is discarded and a new reduce-only close is built from the actual
filled size. Final position and open-order checks remain after the round trip.

Still intentionally limited:

- live fast close still requires `--execute-live` plus exact confirmation
- position REST is used as final verification, not as permission to skip safety
- `cloid` exists in the API but this adapter does not yet assign bot-generated
  client order ids; add that before broader unattended production use
- no unguarded cancel/transfer/withdraw path is implemented

Actual live execution requires the bundled Python where the official Hotstuff SDK is installed, plus both flags:

```powershell
$env:PYTHONPATH="src"
C:\Users\USER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe `
  -m perpdex_farming_bot.cli.hotstuff_live_test `
  --execute-live `
  --confirm LIVE_HOTSTUFF_100_USD_TO_1000
```

Testnet variants add:

```powershell
--environment testnet
```

Reduce-only close for a leftover position requires a market-specific confirmation:

```powershell
$env:PYTHONPATH="src"
$env:HOTSTUFF_ACCOUNT_ADDRESS_PRODUCTION="<owner wallet address>"
C:\Users\USER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe `
  -m perpdex_farming_bot.cli.hotstuff_close_position `
  --market HYPE-PERP `
  --execute-live `
  --confirm CLOSE_HOTSTUFF_HYPE_REDUCE_ONLY
```

The close command uses reduce-only IOC with an aggressive limit price. This is for closing an existing position only; it should not be used as a normal entry path.

## Current Scope

Implemented now:

- Hotstuff endpoint validation
- local env check that prints only `present` or `missing`
- public `/info` allowlist for `instruments`, `ticker`, and `orderbook`
- private read-only `/info` allowlist for account methods such as `accountSummary`, `accountInfo`, `openOrders`, and `positions`
- paper config skeleton with live-order permissions disabled
- live-test preflight that picks the lowest expected-loss eligible market without sending orders
- guarded live-test CLI with exact confirmation required before any order
- guarded reduce-only close CLI for leftover positions
- Hotstuff adapter that contains exchange-specific live order submission
- shared core runner that calls the adapter interface instead of calling the Hotstuff SDK directly
- secret-safe `execution_event_json` output for future GUI/Telegram/SQLite use

Not implemented yet:

- unguarded order placement
- order cancellation
- transfer or withdrawal
- position-changing execution
- live Hotstuff paper cycle
