# Runtime Setup

This computer is the development computer. The future execution server should
install the same repo code and Python dependencies, but it should keep its own
local `.env`, data files, and logs.

## Dependency File

Runtime dependencies are pinned in:

```text
requirements.txt
```

Current SDK policy:

- Hibachi live/read-only SDK: `hibachi-xyz==0.3.1`
- Hotstuff live order SDK: `hotstuff-python-sdk==0.1.1b4`
- Hyperliquid official SDK: `hyperliquid-python-sdk==0.24.0`
- Lighter official SDK: `lighter-sdk==1.1.1`
- RiseX SDK note: official docs point to `risex-client`, an unofficial
  community TypeScript SDK. This Python repo uses direct read-only REST and
  WebSocket skeletons first.
- Pacifica SDK note: official docs point to `pacifica-fi/python-sdk`, a Python
  examples repo for REST/WebSocket and signed order operations. This repo uses
  direct REST/WebSocket code and adds only the signing libraries needed for the
  guarded Pacifica dry-run/live-test CLI.
- Hotstuff signer helper: `eth-account>=0.13,<0.14`
- WebSocket market monitor: `websockets==16.0`
- Pacifica signer helper: `base58>=2.1.1,<3` and `solders>=0.26,<0.27`

Prefer Python 3.13 for the execution server. Hibachi requires Python 3.13 or
newer, and Hotstuff's published SDK metadata currently lists Python support up
to 3.13. Newer Python versions can be used only after a separate read-only and
paper/live-small validation pass.

## Development Computer Install

From the repo root:

```powershell
cd C:\Users\USER\Documents\PerpDEX-Farming-Bot
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If Python 3.13 is not installed on the development computer, keep using the
currently working Python for local code edits, but treat server validation as
the final runtime check.

## Execution Server Install

On the server computer, clone or pull this same repo, then run the same install:

```powershell
cd C:\Users\USER\Documents\PerpDEX-Farming-Bot
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Server-local files must be created separately and must not be committed:

```text
.env
data/
logs/
*.sqlite
```

Do not paste real API keys, private keys, signatures, session keys, or Telegram
tokens into chat, docs, commits, or GitHub. Keep them only in the local `.env`
or OS environment variables on the machine that will run the bot.

## Safe Validation Order

After installing dependencies on either computer:

```powershell
$env:PYTHONPATH="src"
python -m compileall src
python -m perpdex_farming_bot.cli.check_hibachi_env
python -m perpdex_farming_bot.cli.check_hotstuff_env
python -m perpdex_farming_bot.cli.check_hyperliquid_env --environment production
python -m perpdex_farming_bot.cli.check_lighter_env --environment production
python -m perpdex_farming_bot.cli.check_risex_env --environment testnet
python -m perpdex_farming_bot.cli.check_pacifica_env --environment testnet
```

Read-only network checks:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_sdk_smoke --network --public --symbol BTC/USDT-P
python -m perpdex_farming_bot.cli.hotstuff_readonly_smoke --environment production --network
python -m perpdex_farming_bot.cli.hyperliquid_readonly_smoke --environment production --network --public --public-check meta
python -m perpdex_farming_bot.cli.hyperliquid_readonly_smoke --environment production --network --public --public-check l2-book --coin BTC
python -m perpdex_farming_bot.cli.lighter_readonly_smoke --environment production --network --public --public-check markets
python -m perpdex_farming_bot.cli.lighter_readonly_smoke --environment production --network --public --public-check orderbook --market-id 0 --limit 5
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment testnet --network --public --public-check markets
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment testnet --network --public --public-check orderbook --market-id 1
python -m perpdex_farming_bot.cli.pacifica_readonly_smoke --environment testnet --network --public --public-check markets
python -m perpdex_farming_bot.cli.pacifica_readonly_smoke --environment testnet --network --public --public-check orderbook --symbol BTC --agg-level 1
```

Pacifica guarded live-test preparation:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.pacifica_live_preflight --environment testnet
python -m perpdex_farming_bot.cli.pacifica_live_preflight --environment testnet --network --symbol BTC --max-notional-usd 15
python -m perpdex_farming_bot.cli.pacifica_live_test --environment testnet --network --symbol BTC --max-notional-usd 15
```

RiseX guarded dry-run preparation, no live order:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.risex_live_preflight --environment production --network --market-id 1 --max-notional-usd 10
python -m perpdex_farming_bot.cli.risex_live_volume --environment production --network --market-ids 1,2,4,5 --target-gross-volume-usd 20 --max-leg-notional-usd 10 --spread-bps 1 --book-fraction 0.5 --fast-close-on-fill --prebuild-close-order
```

Hyperliquid guarded dry-run preparation, no live order:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hyperliquid_live_preflight --environment production --network --config config/hyperliquid.live-volume.json
python -m perpdex_farming_bot.cli.hyperliquid_live_preflight --environment production --network --fast-close-on-fill --prebuild-close-order
python -m perpdex_farming_bot.cli.hyperliquid_live_test --network --coin BTC
```

The RiseX volume dry-run emits `execution_event_json={...}` for future
Telegram/GUI/SQLite readers. Do not add `--execute-live` to any guarded live
test unless the operator has explicitly approved that tiny live test in the
current session.

Hyperliquid's tiny live BTC roundtrip uses this separate confirmation string:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hyperliquid_live_test --network --coin BTC --execute-live --confirm LIVE_HYPERLIQUID_TINY_BTC_ROUNDTRIP
```

Hyperliquid's spread-gated `$1000` gross-volume test uses this separate
confirmation string:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hyperliquid_live_volume_test --config config/hyperliquid.spread-volume-test.json --network --execute-live --confirm LIVE_HYPERLIQUID_1000_SPREAD_VOLUME
```

Only after env, public read-only, private read-only, paper-only, and dry-run are
clean should live trading be enabled with the explicit CLI confirmation string.
