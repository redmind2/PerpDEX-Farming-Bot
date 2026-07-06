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
- Hotstuff signer helper: `eth-account>=0.13,<0.14`
- WebSocket market monitor: `websockets==16.0`

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
```

Read-only network checks:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_sdk_smoke --network --public --symbol BTC/USDT-P
python -m perpdex_farming_bot.cli.hotstuff_readonly_smoke --environment production --network
```

Only after env, public read-only, private read-only, paper-only, and dry-run are
clean should live trading be enabled with the explicit CLI confirmation string.
