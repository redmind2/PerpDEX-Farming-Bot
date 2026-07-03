from __future__ import annotations

from datetime import datetime, timedelta, timezone

from perpdex_farming_bot.models import MarketSnapshot, QuoteLevel, Side, TradePrint


def mock_snapshot(exchange_id: str, market: str) -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    return MarketSnapshot(
        exchange_id=exchange_id,
        market=market,
        best_bid=QuoteLevel(price=100.00, size=1.2),
        best_ask=QuoteLevel(price=100.02, size=0.8),
        second_bid=QuoteLevel(price=99.995, size=1.4),
        second_ask=QuoteLevel(price=100.025, size=1.1),
        average_spread_bps=3.0,
        tick_size=0.005,
        timestamp=now,
        recent_trades=(
            TradePrint(Side.BUY, 100.02, 0.12, now - timedelta(seconds=4)),
            TradePrint(Side.SELL, 100.00, 0.10, now - timedelta(seconds=3)),
            TradePrint(Side.BUY, 100.02, 0.11, now - timedelta(seconds=2)),
            TradePrint(Side.SELL, 100.00, 0.13, now - timedelta(seconds=1)),
        ),
    )
