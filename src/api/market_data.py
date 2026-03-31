"""src/api/market_data.py — клиент MarketDataService (аналог C++ MarketDataClient).

Подписки:
  - subscribe_bars: SubscribeBars stream → BarCallback
  - subscribe_quotes: SubscribeQuote stream → QuoteCallback
  - subscribe_orderbook: SubscribeOrderBook stream → OrderBookCallback
  - subscribe_latest_trades: SubscribeLatestTrades stream → TradeCallback

Все стримы имеют reconnection-логику через exponential_backoff.
TTL стрима ~86400s — при разрыве переподключаемся автоматически.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

import grpc
import grpc.aio

from core.domain import Symbol, Quote, Bar, OrderBook, OrderBookRow, TradeEvent
from core.errors import AppError, ErrorCode
from core.backoff import exponential_backoff
from core.maintenance import wait_if_maintenance
from auth.manager import AuthManager

logger = logging.getLogger(__name__)

QuoteCallback = Callable[[Quote], None]
BarCallback = Callable[[Bar], None]
OrderBookCallback = Callable[[OrderBook], None]
TradeCallback = Callable[[TradeEvent], None]

# Маппинг таймфреймов в proto enum (заглушка до генерации stubs)
_TF_MAP: dict[str, int] = {
    "M1": 1, "M5": 2, "M15": 3, "M30": 4,
    "H1": 5, "H2": 6, "H4": 7, "H8": 8,
    "D1": 9, "W1": 10, "MN": 11,
}


def _decimal_to_float(d) -> float:
    """google.type.Decimal → float. Finam возвращает строку в поле value."""
    try:
        return float(d.value)
    except (AttributeError, ValueError):
        return 0.0


def _ts_from_proto(ts) -> datetime:
    """google.protobuf.Timestamp → datetime."""
    try:
        return datetime.utcfromtimestamp(ts.seconds + ts.nanos / 1e9)
    except Exception:
        return datetime.utcnow()


def _symbol_from_str(s: str) -> Symbol:
    """'Si-6.26@FORTS' → Symbol."""
    at = s.rfind("@")
    if at == -1:
        return Symbol(security_code=s)
    return Symbol(security_code=s[:at], security_board=s[at + 1:])


class MarketDataClient:
    """Async-клиент MarketDataService Finam Trade API v2."""

    def __init__(self, auth: AuthManager) -> None:
        self._auth = auth
        # Stubs инициализируются лениво при первом использовании
        self._stub = None

    def _get_stub(self):
        if self._stub is None:
            try:
                from proto.grpc_trade_api.v1.marketdata import marketdataservice_pb2_grpc  # type: ignore
                self._stub = marketdataservice_pb2_grpc.MarketDataServiceStub(self._auth.channel)
            except ModuleNotFoundError as exc:
                raise AppError(ErrorCode.INTERNAL, "Proto stubs not found. Run: make proto") from exc
        return self._stub

    def _metadata(self) -> list[tuple[str, str]]:
        return self._auth.metadata()

    # ------------------------------------------------------------------ #
    # Unary                                                                #
    # ------------------------------------------------------------------ #

    async def get_bars(
        self,
        symbol: Symbol,
        timeframe: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[Bar]:
        """
        Исторические бары (unary RPC).

        Raises:
            AppError: при ошибке gRPC.
        """
        try:
            from proto.grpc_trade_api.v1.marketdata import marketdataservice_pb2 as pb  # type: ignore
        except ModuleNotFoundError as exc:
            raise AppError(ErrorCode.INTERNAL, "Proto stubs not found") from exc

        req = pb.BarsRequest(
            symbol=str(symbol),
            timeframe=_TF_MAP.get(timeframe, 1),
        )
        req.interval.start_time.seconds = int(from_dt.timestamp())
        req.interval.end_time.seconds = int(to_dt.timestamp())

        try:
            resp = await self._get_stub().Bars(req, metadata=self._metadata())
        except grpc.aio.AioRpcError as e:
            raise AppError(ErrorCode.RPC_ERROR, f"get_bars: {e.details()}") from e

        return [
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open=_decimal_to_float(b.open),
                high=_decimal_to_float(b.high),
                low=_decimal_to_float(b.low),
                close=_decimal_to_float(b.close),
                volume=int(_decimal_to_float(b.volume)),
                ts=_ts_from_proto(b.timestamp),
                date=datetime.utcfromtimestamp(b.timestamp.seconds).strftime("%Y-%m-%d"),
            )
            for b in resp.bars
        ]

    # ------------------------------------------------------------------ #
    # Streaming subscriptions                                              #
    # ------------------------------------------------------------------ #

    async def subscribe_bars(
        self,
        symbol: Symbol,
        timeframe: str,
        callback: BarCallback,
        stop_event: asyncio.Event,
    ) -> None:
        """
        Подписка на бары. Запускается как asyncio.Task.
        Reconnect при разрыве через exponential_backoff.
        """
        try:
            from proto.grpc_trade_api.v1.marketdata import marketdataservice_pb2 as pb  # type: ignore
        except ModuleNotFoundError as exc:
            raise AppError(ErrorCode.INTERNAL, "Proto stubs not found") from exc

        async for attempt in exponential_backoff():
            if stop_event.is_set():
                return
            await wait_if_maintenance(stop_event)
            if stop_event.is_set():
                return
            try:
                req = pb.SubscribeBarsRequest(
                    symbol=str(symbol),
                    timeframe=_TF_MAP.get(timeframe, 1),
                )
                async for resp in self._get_stub().SubscribeBars(req, metadata=self._metadata()):
                    for b in resp.bars:
                        callback(Bar(
                            symbol=symbol,
                            timeframe=timeframe,
                            open=_decimal_to_float(b.open),
                            high=_decimal_to_float(b.high),
                            low=_decimal_to_float(b.low),
                            close=_decimal_to_float(b.close),
                            volume=int(_decimal_to_float(b.volume)),
                            ts=_ts_from_proto(b.timestamp),
                        ))
            except grpc.aio.AioRpcError as e:
                if stop_event.is_set():
                    return
                logger.warning(
                    "[MD] bars stream [%s/%s] error: %s, retry in %.1fs",
                    symbol, timeframe, e.details(), attempt.delay,
                )
            except asyncio.CancelledError:
                return

    async def subscribe_quotes(
        self,
        symbols: list[Symbol],
        callback: QuoteCallback,
        stop_event: asyncio.Event,
    ) -> None:
        """Подписка на котировки (bid/ask/last)."""
        try:
            from proto.grpc_trade_api.v1.marketdata import marketdataservice_pb2 as pb  # type: ignore
        except ModuleNotFoundError as exc:
            raise AppError(ErrorCode.INTERNAL, "Proto stubs not found") from exc

        async for attempt in exponential_backoff():
            if stop_event.is_set():
                return
            await wait_if_maintenance(stop_event)
            try:
                req = pb.SubscribeQuoteRequest(symbols=[str(s) for s in symbols])
                async for resp in self._get_stub().SubscribeQuote(req, metadata=self._metadata()):
                    for q in resp.quote:
                        callback(Quote(
                            symbol=_symbol_from_str(q.symbol),
                            bid=_decimal_to_float(q.bid),
                            ask=_decimal_to_float(q.ask),
                            last=_decimal_to_float(q.last),
                            volume=int(_decimal_to_float(q.last_size)),
                            ts=_ts_from_proto(q.timestamp),
                        ))
            except grpc.aio.AioRpcError as e:
                if stop_event.is_set():
                    return
                logger.warning("[MD] quote stream error: %s, retry in %.1fs", e.details(), attempt.delay)
            except asyncio.CancelledError:
                return

    async def subscribe_orderbook(
        self,
        symbol: Symbol,
        callback: OrderBookCallback,
        stop_event: asyncio.Event,
    ) -> None:
        """Подписка на стакан. Инкрементальный апдейт → накопление снапшота."""
        try:
            from proto.grpc_trade_api.v1.marketdata import marketdataservice_pb2 as pb  # type: ignore
        except ModuleNotFoundError as exc:
            raise AppError(ErrorCode.INTERNAL, "Proto stubs not found") from exc

        # Накопленный стакан
        bids_map: dict[float, int] = {}
        asks_map: dict[float, int] = {}

        async for attempt in exponential_backoff():
            if stop_event.is_set():
                return
            bids_map.clear()
            asks_map.clear()
            try:
                req = pb.SubscribeOrderBookRequest(symbol=str(symbol))
                async for resp in self._get_stub().SubscribeOrderBook(req, metadata=self._metadata()):
                    for sob in resp.order_book:
                        for row in sob.rows:
                            price = _decimal_to_float(row.price)
                            # ACTION_REMOVE или qty==0 → удалить уровень
                            if row.HasField("buy_size"):
                                qty = _decimal_to_float(row.buy_size)
                                if row.action == pb.StreamOrderBookRow.ACTION_REMOVE or qty < 1e-9:
                                    bids_map.pop(price, None)
                                else:
                                    bids_map[price] = int(qty)
                            elif row.HasField("sell_size"):
                                qty = _decimal_to_float(row.sell_size)
                                if row.action == pb.StreamOrderBookRow.ACTION_REMOVE or qty < 1e-9:
                                    asks_map.pop(price, None)
                                else:
                                    asks_map[price] = int(qty)
                        book = OrderBook(
                            symbol=symbol,
                            bids=[OrderBookRow(p, q) for p, q in sorted(bids_map.items(), reverse=True)],
                            asks=[OrderBookRow(p, q) for p, q in sorted(asks_map.items())],
                        )
                        callback(book)
            except grpc.aio.AioRpcError as e:
                if stop_event.is_set():
                    return
                logger.warning("[MD] orderbook stream error: %s, retry in %.1fs", e.details(), attempt.delay)
            except asyncio.CancelledError:
                return

    async def subscribe_latest_trades(
        self,
        symbol: Symbol,
        callback: TradeCallback,
        stop_event: asyncio.Event,
    ) -> None:
        """Подписка на ленту сделок."""
        try:
            from proto.grpc_trade_api.v1.marketdata import marketdataservice_pb2 as pb  # type: ignore
        except ModuleNotFoundError as exc:
            raise AppError(ErrorCode.INTERNAL, "Proto stubs not found") from exc

        last_price = 0.0
        async for attempt in exponential_backoff():
            if stop_event.is_set():
                return
            try:
                req = pb.SubscribeLatestTradesRequest(symbol=str(symbol))
                async for resp in self._get_stub().SubscribeLatestTrades(req, metadata=self._metadata()):
                    for t in resp.trades:
                        price = _decimal_to_float(t.price)
                        vol = _decimal_to_float(t.size)
                        # Если сторона не определена — по направлению цены
                        if t.side == pb.SIDE_BUY:
                            is_buy = True
                        elif t.side == pb.SIDE_SELL:
                            is_buy = False
                        else:
                            is_buy = price >= last_price
                        last_price = price
                        callback(TradeEvent(price=price, volume=vol, is_buy=is_buy, ts=_ts_from_proto(t.timestamp)))
            except grpc.aio.AioRpcError as e:
                if stop_event.is_set():
                    return
                logger.warning("[MD] trades stream error: %s, retry in %.1fs", e.details(), attempt.delay)
            except asyncio.CancelledError:
                return
