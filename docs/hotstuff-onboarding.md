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
- live-test preflight that picks the lowest-spread eligible market without sending orders
- guarded live-test CLI with exact confirmation required before any order
- guarded reduce-only close CLI for leftover positions
- Hotstuff adapter that contains exchange-specific live order submission
- shared core runner that calls the adapter interface instead of calling the Hotstuff SDK directly

Not implemented yet:

- unguarded order placement
- order cancellation
- transfer or withdrawal
- position-changing execution
- live Hotstuff paper cycle
