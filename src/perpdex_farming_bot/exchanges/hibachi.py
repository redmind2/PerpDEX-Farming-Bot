from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from perpdex_farming_bot.cli.hibachi_live_roundtrip import _execute_paired_market_batch, _position_state
from perpdex_farming_bot.connectors.hibachi_readonly import (
    DEFAULT_HIBACHI_API_ENDPOINT,
    DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    endpoint_from_env,
)
from perpdex_farming_bot.credentials import read_hibachi_credentials
from perpdex_farming_bot.env import get_env
from perpdex_farming_bot.exchanges.base import ExchangeOrderResult, ExchangePosition, PairedRoundtripResult


@dataclass(frozen=True)
class HibachiAdapter:
    credential_prefix: str
    max_fees_percent: Decimal = Decimal("0.0005")

    exchange_id: str = "hibachi"

    def list_positions(self) -> tuple[ExchangePosition, ...]:
        client = self._client()
        account_info = client.get_account_info()
        positions: list[ExchangePosition] = []
        for position in getattr(account_info, "positions", ()):
            market = str(getattr(position, "symbol", ""))
            if not market:
                continue
            direction, quantity = _position_state(account_info, market)
            if quantity <= Decimal("0"):
                continue
            signed_size = quantity if direction == "long" else -quantity
            positions.append(ExchangePosition(self.exchange_id, market, signed_size, direction or "unknown"))
        return tuple(positions)

    def execute_paired_notional_roundtrip(
        self,
        *,
        market: str,
        instrument_id: int,
        buy_price: Decimal,
        sell_price: Decimal,
        buy_size: Decimal,
        sell_size: Decimal,
        planned_gross_volume_usd: Decimal,
    ) -> PairedRoundtripResult:
        del instrument_id, buy_price, sell_price
        if buy_size != sell_size:
            return PairedRoundtripResult(
                self.exchange_id,
                market,
                False,
                planned_gross_volume_usd,
                status="hibachi_adapter_requires_equal_buy_sell_size",
            )

        client = self._client()
        args = _HibachiBatchArgs(max_fees_percent=self.max_fees_percent)
        assignment = _Assignment(market=market)
        status = _execute_paired_market_batch(client, assignment, args, buy_size, "BUY", "SELL")
        return PairedRoundtripResult(
            self.exchange_id,
            market,
            status == "ok_flat",
            planned_gross_volume_usd,
            status=status,
        )

    def close_position_reduce_only(
        self,
        *,
        market: str,
        instrument_id: int,
        side: str,
        price: Decimal,
        size: Decimal,
    ) -> ExchangeOrderResult:
        del instrument_id, price
        from hibachi_xyz.types import OrderFlags, Side

        client = self._client()
        close_side = Side.BUY if side.lower() in {"b", "buy"} else Side.SELL
        nonce, order_id = client.place_market_order(
            market,
            str(size),
            close_side,
            self.max_fees_percent,
            order_flags=OrderFlags.ReduceOnly,
        )
        return ExchangeOrderResult(
            self.exchange_id,
            market,
            True,
            "submitted",
            exchange_order_id=str(order_id or nonce),
        )

    def _client(self) -> object:
        from hibachi_xyz import HibachiApiClient

        credentials = read_hibachi_credentials(self.credential_prefix)
        api_endpoint = endpoint_from_env(get_env("HIBACHI_API_ENDPOINT_PRODUCTION"), DEFAULT_HIBACHI_API_ENDPOINT)
        data_endpoint = endpoint_from_env(
            get_env("HIBACHI_DATA_API_ENDPOINT_PRODUCTION"),
            DEFAULT_HIBACHI_DATA_API_ENDPOINT,
        )
        return HibachiApiClient(
            api_url=api_endpoint,
            data_api_url=data_endpoint,
            api_key=credentials["api_key"],
            account_id=credentials["account_id"],
            private_key=credentials["private_key"],
        )


@dataclass(frozen=True)
class _Assignment:
    market: str


@dataclass(frozen=True)
class _HibachiBatchArgs:
    max_fees_percent: Decimal
    fill_lookup_attempts: int = 5
    fill_lookup_delay_seconds: float = 0.25
    residual_settle_attempts: int = 5
    residual_settle_delay_seconds: float = 0.25
    skip_fill_spread_lookup: bool = False
