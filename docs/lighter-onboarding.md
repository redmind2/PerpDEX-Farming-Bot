# Lighter Onboarding

This is the Phase 0 Lighter adapter plan for the canonical Farming Bot repo:

```text
C:\Users\USER\Documents\PerpDEX-Farming-Bot
```

Phase 0 means read-only setup only. It must not place orders, cancel orders,
transfer funds, withdraw funds, or change positions.

## Official References

- Get Started: https://apidocs.lighter.xyz/docs/get-started
- SDK: https://apidocs.lighter.xyz/docs/repos
- Python SDK: https://github.com/elliottech/lighter-python
- PyPI package: https://pypi.org/project/lighter-sdk/
- API keys: https://apidocs.lighter.xyz/docs/api-keys
- Signing transactions: https://apidocs.lighter.xyz/docs/trading
- WebSocket: https://apidocs.lighter.xyz/docs/websocket-reference
- Rate limits: https://apidocs.lighter.xyz/docs/rate-limits
- Volume quota: https://apidocs.lighter.xyz/docs/volume-quota-program

## Confirmed Endpoints

Production REST:

```text
https://mainnet.zklighter.elliot.ai
```

WebSocket:

```text
wss://mainnet.zklighter.elliot.ai/stream
wss://testnet.zklighter.elliot.ai/stream
```

The official docs and current Python SDK OpenAPI file confirm the production
REST base URL. This pass did not find a confirmed official testnet REST base URL,
so `LIGHTER_API_ENDPOINT_TESTNET` stays blank in `.env.example` until it is
verified.

## SDK Status

Official docs install the Python SDK with:

```powershell
python -m pip install lighter-sdk
```

PyPI shows `lighter-sdk==1.1.1` as the latest release checked on 2026-07-07, so
`requirements.txt` pins that version. The Phase 0 code still uses direct
read-only REST calls and does not initialize the SDK signer.

## Local Env

Real values belong only in local `.env` or OS environment variables. Never put
real keys, auth tokens, signatures, or wallet-private data in chat, docs, Git,
or `.env.example`.

Production placeholders:

```text
LIGHTER_API_ENDPOINT_PRODUCTION=https://mainnet.zklighter.elliot.ai
LIGHTER_WSS_ENDPOINT_PRODUCTION=wss://mainnet.zklighter.elliot.ai/stream
LIGHTER_L1_ADDRESS_PRODUCTION=
LIGHTER_ACCOUNT_INDEX_PRODUCTION=
LIGHTER_API_KEY_INDEX_PRODUCTION=
LIGHTER_API_PRIVATE_KEY_PRODUCTION=
LIGHTER_READ_ONLY_AUTH_TOKEN_PRODUCTION=
```

Testnet placeholders:

```text
LIGHTER_API_ENDPOINT_TESTNET=
LIGHTER_WSS_ENDPOINT_TESTNET=wss://testnet.zklighter.elliot.ai/stream
LIGHTER_L1_ADDRESS_TESTNET=
LIGHTER_ACCOUNT_INDEX_TESTNET=
LIGHTER_API_KEY_INDEX_TESTNET=
LIGHTER_API_PRIVATE_KEY_TESTNET=
LIGHTER_READ_ONLY_AUTH_TOKEN_TESTNET=
```

`LIGHTER_L1_ADDRESS_*` can be used to discover account indexes with
`accountsByL1Address`. `LIGHTER_ACCOUNT_INDEX_*` is the integer account id used
by most account/order endpoints. `LIGHTER_READ_ONLY_AUTH_TOKEN_*` is optional
but required for auth-gated read-only endpoints such as active orders and
account limits.

## Safe Validation Order

Local env check, no network:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.check_lighter_env --environment production
```

Prepared read-only smoke, no network:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.lighter_readonly_smoke --environment production
```

Public read-only network checks:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.lighter_readonly_smoke --environment production --network --public --public-check markets
python -m perpdex_farming_bot.cli.lighter_readonly_smoke --environment production --network --public --public-check market-details --market-id 0
python -m perpdex_farming_bot.cli.lighter_readonly_smoke --environment production --network --public --public-check orderbook --market-id 0 --limit 5
```

Private read-only checks:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.lighter_readonly_smoke --environment production --network --private-readonly --private-check accounts-by-l1
python -m perpdex_farming_bot.cli.lighter_readonly_smoke --environment production --network --private-readonly --private-check account
python -m perpdex_farming_bot.cli.lighter_readonly_smoke --environment production --network --private-readonly --private-check active-orders --market-id 0
```

The active-orders and trades checks need `LIGHTER_READ_ONLY_AUTH_TOKEN_PRODUCTION`
when run through the read-only smoke CLI. The guarded live-test CLI generates a
short-lived auth token in memory instead of requiring it in `.env`.

## Guarded Tiny BTC Live Roundtrip

The dedicated live-test CLI is still guarded. Without both `--execute-live` and
the exact confirmation text, it only runs read-only preflight and prints
`live_ready=True`.

Read-only preflight:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.lighter_live_test --environment production --network --quote-amount-usd 25 --max-notional-usd 30
```

Live BTC roundtrip, after explicit approval:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.lighter_live_test --environment production --network --quote-amount-usd 25 --max-notional-usd 30 --execute-live --confirm LIVE_LIGHTER_TINY_BTC_ROUNDTRIP
```

Faster close experiment, after explicit approval:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.lighter_live_test --environment production --network --quote-amount-usd 25 --max-notional-usd 30 --close-mode optimistic --optimistic-close-delay-seconds 0.15 --execute-live --confirm LIVE_LIGHTER_TINY_BTC_ROUNDTRIP
```

Netting roundtrip for volume farming, after explicit approval:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.lighter_live_test --environment production --network --quote-amount-usd 25 --max-notional-usd 30 --close-mode netting --execute-live --confirm LIVE_LIGHTER_TINY_BTC_ROUNDTRIP
```

Safety behavior:

- Blocks unless the Lighter signing env is present.
- Generates a short-lived auth token in memory for read-only active-order checks.
- Blocks when existing positions or open orders are detected.
- Rechecks the BTC orderbook immediately before entry and close.
- Sends market IOC buy, then a reduce-only market IOC sell after the position is detected.
- The default confirmed mode polls every 0.1 seconds for faster position detection.
- `--close-mode optimistic` sends a reduce-only close from the entry plan after a short delay, then checks final state and uses confirmed rescue close if a position remains.
- `--close-mode netting` sends the opposite market IOC without reduce-only as soon as the entry submit returns, then checks final state and uses reduce-only rescue close if a position remains.
- Prints final flat/open-order state and matched trade fee fields without printing secrets.

## Phase 0 Files

- env check: `src/perpdex_farming_bot/cli/check_lighter_env.py`
- read-only smoke: `src/perpdex_farming_bot/cli/lighter_readonly_smoke.py`
- guarded live test: `src/perpdex_farming_bot/cli/lighter_live_test.py`
- read-only connector: `src/perpdex_farming_bot/connectors/lighter_readonly.py`
- market data helper: `src/perpdex_farming_bot/marketdata/lighter.py`
- adapter skeleton: `src/perpdex_farming_bot/exchanges/lighter.py`
- fee provider skeleton: `src/perpdex_farming_bot/exchanges/lighter_fees.py`

## Safety Notes

- Lighter API keys can read auth-gated data and can also authorize write flows.
  Treat the private key and generated auth token as sensitive.
- API key indexes are account-specific. Do not guess or auto-create key indexes.
- Official docs reserve low key indexes for app interfaces. Use only the index
  shown by Lighter after explicit setup.
- Transaction nonce management is per API key. Phase 0 does not sign or send
  transactions, so nonce logic is intentionally not implemented here.
- REST and WebSocket limits apply to IP and L1 address. Keep polling light.
- WebSocket clients must send a ping frame or other message at least every two
  minutes to keep the connection alive.
- Volume quota is related to `SendTx` and `SendTxBatch`; Phase 0 does not use
  either endpoint.

## Fee Handling

The fee provider reads market metadata from `GET /api/v1/orderBooks`. Lighter
documents maker/taker fee fields there as percentages, so the provider converts
percentage to bps only when those fields are present. If no complete fee source
is available, the provider returns `fee_unknown` and blocks trading candidates.
It never assumes zero fee.
