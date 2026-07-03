# Data Hub Read-Only Contract

This bot may read public market data from the Dashboard/Data Hub SQLite DB.

It must never write to the Data Hub DB.

## Boundary

Data Hub project:

- public market data only
- no wallet connection
- no private key
- no API key
- no signature or session key
- no balance or position management
- no order, cancel, or trade execution

Trading bot project:

- strategy decisions
- paper/live intent logs
- private read-only checks
- order execution only after explicit approval
- bot-owned SQLite ledger and bot logs

## Server Data Source

Server DB path:

```text
C:\Users\redmi\Documents\PerpDEX-Dashboard-Data-Hub\data\live-5m-server.sqlite
```

Python read-only connection shape:

```python
sqlite3.connect(
    "file:C:/Users/redmi/Documents/PerpDEX-Dashboard-Data-Hub/data/live-5m-server.sqlite?mode=ro",
    uri=True,
    timeout=30,
)
```

The bot connector opens the DB with `mode=ro` and short read queries.

## Runtime Rules

- Do not write to the Data Hub DB.
- Do not copy, edit, or replace the DB, `-wal`, or `-shm` files.
- Store bot decisions, paper fills, real fills, and logs in the bot's own DB/logs.
- SQLite WAL mode allows collector writes and bot reads at the same time.
- Keep read transactions short.
- Retry briefly on `database locked`.

## Spread Signal Rules

Collection cadence is 5 minutes:

```text
PERPDEX_COLLECTION_INTERVAL=300
```

The bot uses the Data Hub DB as a spread signal source only.

Hibachi uses two symbol names in this project:

- `market`: Hibachi SDK symbol used for public orderbook reads, for example `BTC/USDT-P`.
- `data_hub_symbol`: Dashboard/Data Hub SQLite symbol used for spread lookup, for example `BTC-PERP`.

Data Hub no-trade rules:

- Data Hub DB cannot be opened: no-trade
- latest spread snapshot is missing: no-trade
- Data Hub spread is above the average-spread threshold: no-trade
- Data Hub spread is above `max_spread_bps` when that cap is greater than `0`: no-trade

Orderbook depth is not judged from Data Hub. The bot should fetch live orderbook from the exchange API immediately before paper or live execution.

## Tables Used

`market_snapshots`:

- `id`
- `exchange_id`
- `symbol`
- `timestamp`
- `mark_price`
- `index_price`
- `best_bid`
- `best_ask`
- `spread`
- `spread_bps`

`orderbook_levels`:

- `id`
- `snapshot_id`
- `side`
- `price`
- `size`
- `notional`
- `level_index`

The trading bot does not need this table for the first Hibachi strategy. Real-time depth should come from Hibachi public orderbook API.

`funding_rates`:

- `id`
- `exchange_id`
- `symbol`
- `timestamp`
- `rate`
- `next_funding_time`

`collector_market_status`:

- `exchange_id`
- `symbol`
- `last_success_at`
- `last_failure_at`
- `consecutive_failures`
- `last_error`
- `next_collection_at`
- `updated_at`

## Bot Command

On the server, run paper cycle with Data Hub data:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle `
  --data-source data-hub `
  --orderbook-source hibachi-sdk `
  --network `
  --data-hub-db "C:\Users\redmi\Documents\PerpDEX-Dashboard-Data-Hub\data\live-5m-server.sqlite"
```

For a local copied/static DB snapshot, add `--data-hub-immutable` if Windows or sandbox file locks prevent a normal read-only open. Do not use that flag for a DB that is actively being written by the live collector.

Or set local `.env` / environment variable:

```text
PERPDEX_DATA_HUB_DB=C:\Users\redmi\Documents\PerpDEX-Dashboard-Data-Hub\data\live-5m-server.sqlite
```

Then:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle --data-source data-hub
```

CLI commands from the Data Hub project are for human debugging. The trading bot should prefer direct read-only SQLite queries.
