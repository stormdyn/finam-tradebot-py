import asyncio
import logging
from typing import Callable, Optional, AsyncIterator
import grpc
from grpc.aio import Channel, UnaryStreamCall
from .grpc_stubs.marketdata_service_pb2 import (
    BarsRequest, BarsResponse,
    SubscribeQuoteRequest, SubscribeQuoteResponse,
    SubscribeOrderBookRequest, SubscribeOrderBookResponse,
    TimeFrame as ProtoTimeFrame
)
from .grpc_stubs.marketdata_service_pb2_grpc import MarketDataServiceStub
from src.core.domain import Symbol, Quote, Bar, OrderUpdate, Signal
from ..core.errors import Result, AppError, ErrorCode
from ..auth.manager import AuthManager
from ..core.backoff import ExponentialBackoff
from ..core.maintenance import wait_if_maintenance

logger = logging.getLogger(__name__)

QuoteCallback = Callable[[Quote], None]
BarCallback = Callable[[Bar], None]
OrderBookCallback = Callable[[OrderBook], None]

class Subscription:
    """Контекстный менеджер для отписки"""
    def __init__(self, cancel: Callable[[], None]):
        self._cancel = cancel
    def __enter__(self): return self
    def __exit__(self, *args): self._cancel()
    def cancel(self): self._cancel()

class MarketDataClient:
    def __init__(self, auth: AuthManager):
        self._auth = auth
        self._channel: Optional[Channel] = None
        self._stub: Optional[MarketDataServiceStub] = None
    
    async def connect(self) -> Result[None]:
        try:
            from grpc.aio import secure_channel
            creds = grpc.ssl_channel_credentials()
            self._channel = secure_channel("api.finam.ru:443", creds)
            self._stub = MarketDataServiceStub(self._channel)
            return None
        except Exception as e:
            return AppError(ErrorCode.CONNECTION_FAILED, str(e))
    
    async def get_bars(
        self,
        symbol: Symbol,
        timeframe: str,  # "D1", "M1", etc.
        from_ts: datetime,
        to_ts: datetime
    ) -> Result[list[Bar]]:
        """Unary RPC: исторические бары"""
        req = BarsRequest(
            symbol=str(symbol),
            timeframe=self._tf_to_proto(timeframe),
            interval=TimeInterval(
                start_time=Timestamp(seconds=int(from_ts.timestamp())),
                end_time=Timestamp(seconds=int(to_ts.timestamp()))
            )
        )
        try:
            resp: BarsResponse = await self._stub.Bars(
                req, metadata=self._auth._make_metadata()
            )
            bars = [self._proto_to_bar(b, symbol, timeframe) for b in resp.bars]
            return bars
        except grpc.RpcError as e:
            return AppError(ErrorCode.RPC_ERROR, f"get_bars: {e.details()}")
    
    def subscribe_quotes(
        self,
        symbols: list[Symbol],
        callback: QuoteCallback
    ) -> Subscription:
        """Streaming RPC: котировки в реальном времени"""
        stop_flag = asyncio.Event()
        
        async def _stream_loop():
            backoff = ExponentialBackoff()
            while not stop_flag.is_set():
                await wait_if_maintenance(stop_flag)  # ждём 05:00-06:15 MSK
                try:
                    req = SubscribeQuoteRequest(symbols=[str(s) for s in symbols])
                    call: UnaryStreamCall = self._stub.SubscribeQuote(
                        req, metadata=self._auth._make_metadata()
                    )
                    async for resp in call:
                        for q in resp.quote:
                            callback(self._proto_to_quote(q))
                        backoff.reset()
                except grpc.RpcError as e:
                    if e.code() == grpc.StatusCode.CANCELLED:
                        break
                    logger.warning(f"[MD] quote stream error: {e.code()}")
                if not stop_flag.is_set():
                    await backoff.wait(stop_flag)
        
        task = asyncio.create_task(_stream_loop())
        return Subscription(lambda: stop_flag.set() or task.cancel())
    
    # ... аналогично subscribe_bars, subscribe_order_book ...
    
    @staticmethod
    def _tf_to_proto(tf: str) -> ProtoTimeFrame:
        mapping = {"M1": ProtoTimeFrame.TIME_FRAME_M1, "D1": ProtoTimeFrame.TIME_FRAME_D}
        return mapping.get(tf, ProtoTimeFrame.TIME_FRAME_M1)
    
    @staticmethod
    def _proto_to_bar(pb, symbol: Symbol, tf: str) -> Bar:
        from google.type import decimal_pb2
        def dec_to_float(d: decimal_pb2.Decimal) -> float:
            return float(d.value) if d.value else 0.0
        return Bar(
            symbol=symbol, timeframe=tf,
            open=dec_to_float(pb.open), high=dec_to_float(pb.high),
            low=dec_to_float(pb.low), close=dec_to_float(pb.close),
            volume=int(dec_to_float(pb.volume)),
            ts=datetime.fromtimestamp(pb.timestamp.seconds),
            date=datetime.fromtimestamp(pb.timestamp.seconds).strftime("%Y-%m-%d")
        )