# Security and Multi-Account Design

## Sensitive Information Rules

Never store real secrets in code or committed config files.

Do not commit:

- API key
- API secret
- private key
- seed phrase
- signature
- session key
- wallet export file

Allowed in the repo:

- environment variable names
- credential prefixes such as `MOCK_EXCHANGE_PAPER_1`
- account IDs such as `paper_account_1`
- wallet IDs such as `paper_wallet_1`
- safe examples with blank values

Real values belong only in local environment variables or a local `.env` file that is ignored by git.

## Credential Prefix Rule

The bot treats credential prefixes case-insensitively.

These all refer to the same credential set:

```text
hibachi_1
HIBACHI_1
Hibachi_1
```

They map to:

```text
HIBACHI_1_API_KEY_PRODUCTION
HIBACHI_1_PUBLIC_KEY_PRODUCTION
HIBACHI_1_PRIVATE_KEY_PRODUCTION
HIBACHI_1_ACCOUNT_ID_PRODUCTION
```

Config files should store logical account IDs and credential prefixes, not real API key values.

## Single-Exchange / Multi-Wallet Model

The bot should treat every strategy run as this tuple:

```text
exchange_id + account_id + wallet_id + market + strategy
```

`exchange_id` is still kept as an identifier, but this repository does not run cross-exchange strategies.

Allowed here:

- one exchange at a time
- multiple wallets/accounts on that same exchange
- multiple markets when assignments do not overlap
- paired wallets on the same exchange for delta-neutral paper strategies

Not allowed here:

- exchange A long and exchange B short as one strategy
- cross-exchange arbitrage
- cross-exchange inventory balancing
- cross-exchange transfer/rebalance logic

Those belong in a separate `Cross-Exchange-Trading-Bot`.

## Strategy Assignment Rule

Each active strategy must have an explicit assignment.

Example:

```json
{
  "assignment_id": "mock_exchange.paper_account_1.BTC-PERP.market-market",
  "exchange_id": "mock_exchange",
  "account_id": "paper_account_1",
  "wallet_id": "paper_wallet_1",
  "market": "BTC-PERP",
  "strategy": "market-market",
  "enabled": true
}
```

Before live execution, the bot should enforce a lock so that two strategies cannot control the same `exchange_id + account_id + market` at the same time.

## Paired Delta-Neutral Assignment

Some strategies intentionally use two wallets on the same exchange and same market.

Example:

```text
wallet A: long BTC-PERP
wallet B: short BTC-PERP
```

This should still be represented as one strategy assignment, not as two unrelated strategies.

The assignment has:

- primary account/wallet
- paired account/wallet
- one exchange
- one market
- one strategy name

The important boundary is that wallet A and wallet B must not fill each other's orders. They should both interact with external market liquidity.

## Paired Delta-Neutral Strategy Shape

The planned loop is:

1. Wait until spread is below the configured threshold.
2. Open equal notional long/short exposure across two wallets.
3. Stop adding when the paired position cap is reached.
4. Hold for the configured minimum time.
5. Wait until spread is low again.
6. Close both sides with reduce-only paper intents.
7. Repeat only if period/round budget still allows it.

This is still paper-only in the current code.

## Paired Sizing

The paired strategy can size exposure in two ways:

- fixed USD amount
- percentage of total collateral

When both are set, the bot uses the smaller value.

Example:

```json
{
  "delta_neutral_total_collateral_usd": 1000,
  "delta_neutral_notional_cap_usd": 20,
  "delta_neutral_notional_pct_of_collateral": 0.02,
  "delta_neutral_max_pair_position_usd": 100,
  "delta_neutral_max_pair_position_pct_of_collateral": 0.1
}
```

In this example:

- one entry round is capped at the smaller of 20 USD or 2% of collateral
- total paired exposure is capped at the smaller of 100 USD or 10% of collateral
- percentages are written as decimals, so `0.02` means 2%

## Self-Cross Boundary

Market taker flow against external liquidity can be allowed when it pays real fees and follows the exchange or foundation rules.

Still prohibited:

- filling your own limit order with your own market order
- cross-trading between your own accounts
- hiding order patterns to avoid detection
- storing private credentials in the repo
