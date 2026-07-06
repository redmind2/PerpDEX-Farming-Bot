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

The core runner lives at:

- `src/perpdex_farming_bot/core/live_volume.py`

The core runner does not import Hotstuff or Hibachi SDKs directly. Exchange-specific order submission belongs inside adapters.

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

## Telegram Role

Telegram remains a secondary remote:

- status
- balance
- volume
- pause/resume
- health

Telegram must not start live trading directly.
