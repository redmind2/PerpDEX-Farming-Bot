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
- Order rate limits: https://docs.risechain.com/docs/risex/api/order-rate-limits
- Important notes: https://docs.risechain.com/docs/risex/api/important-notes

## Confirmed Shape

- REST public market data exists:
  - `GET /v1/markets`
  - `GET /v1/orderbook?market_id=...`
- WebSocket public orderbook data exists:
  - testnet: `wss://ws.testnet.rise.trade`
  - channel: `orderbook`
- Private read-only account checks can start with:
  - `GET /v1/account/balance?account=...`
  - `GET /v1/positions?account=...`
- Signer/authenticated order flow uses EIP-712 and a session signer.
- `risex-client` exists, but docs describe it as an unofficial community TypeScript SDK. This Python repo should use direct REST/WebSocket skeletons until a Python SDK or a deliberate TypeScript helper boundary is chosen.
- Orders use `market_id`, integer size steps, integer price ticks, and signed permit data. Do not guess conversion rules; use `market.config.step_size` and `market.config.step_price`.

## Safe Commands

Local env check, no network:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.check_risex_env --environment testnet
```

Prepared read-only smoke, no network:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment testnet
```

Public read-only network smoke:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment testnet --network --public --public-check markets
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment testnet --network --public --public-check orderbook --market-id 1
```

Private read-only network smoke requires only the account address in local `.env`:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.risex_readonly_smoke --environment testnet --network --private-readonly --private-check positions
```

Do not paste real account addresses, signer keys, signatures, session keys, or tokens into chat.

## Live Boundary

The Phase 0 adapter blocks live order methods with `AdapterError`.

Before any live order path is implemented, confirm:

1. env check
2. public read-only
3. private read-only
4. paper-only
5. dry-run
6. explicit user approval for a tiny live test

Any future live path must re-check the latest orderbook/depth immediately before order submission and must keep order submission inside `src/perpdex_farming_bot/exchanges/risex.py`.
