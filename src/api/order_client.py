"""OrderExecutor — async gRPC wrapper for Finam OrdersService.

Handles:
  - submit / cancel (unary)
  - SubscribeOrders stream with reconnect + maintenance-window awareness
  - local order-id → exchange order-id mapping
  - risk check delegation before every submit
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import grpc

from core.domain import OrderRequest, OrderSide, OrderStatus, OrderType, OrderUpdate, Symbol
from core.errors import ErrorCode, FinamError
from core.interfaces import IOrderExecutor, IRiskManager
from core.backoff import exponential_backoff
from core.maintenance import wait_if_maintenance
from auth.manager import AuthManager

# Lazy import of generated stubs — generated into proto/ at build time.
try:
    from proto.grpc_trade_api.v1.orders import ordersservice_pb2 as _pb
    from proto.grpc_trade_api.v1.orders import ordersservice_pb2_grpc as _grpc
except ModuleNotFoundError:  # stubs not yet generated
    _pb = None  # type: ignore
    _grpc = None  # type: ignore

log = logging.getLogger(__name__)

OrderUpdateCallback = Callable[[OrderUpdate], None]


class _OrderState:
    __slots__ = ("exchange_id", "symbol", "side", "status", "type",
                 "price", "qty_total", "qty_filled")

    def __init__(
        self,
        exchange_id: str,
        symbol: Symbol,
        side: OrderSide,
        status: OrderStatus,
        order_type: OrderType,
        price: float,
        qty_total: int,
    ) -> None:
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.side = side
        self.status = status
        self.type = order_type
        self.price = price
        self.qty_total = qty_total
        self.qty_filled = 0


def _validate(req: OrderRequest) -> None:
    if not req.client_id:
        raise FinamError(ErrorCode.INVALID_ARGUMENT, "client_id is empty")
    if req.quantity <= 0:
        raise FinamError(ErrorCode.INVALID_QUANTITY,
                         f"quantity must be > 0, got {req.quantity}")
    if req.order_type == OrderType.LIMIT and req.price <= 0.0:
        raise FinamError(ErrorCode.INVALID_PRICE,
                         "limit order requires price > 0")


def _proto_side(side: OrderSide):
    from proto.grpc_trade_api.v1 import common_pb2  # noqa: PLC0415
    return common_pb2.SIDE_BUY if side == OrderSide.BUY else common_pb2.SIDE_SELL


def _proto_order_type(ot: OrderType):
    return _pb.ORDER_TYPE_MARKET if ot == OrderType.MARKET else _pb.ORDER_TYPE_LIMIT


def _map_status(proto_status: int) -> OrderStatus:
    """Map Finam proto OrderStatus enum → domain OrderStatus."""
    _PENDING = {
        _pb.ORDER_STATUS_NEW,
        _pb.ORDER_STATUS_PENDING_NEW,
        _pb.ORDER_STATUS_PENDING_CANCEL,
        _pb.ORDER_STATUS_SUSPENDED,
        _pb.ORDER_STATUS_WATCHING,
        _pb.ORDER_STATUS_WAIT,
        _pb.ORDER_STATUS_LINK_WAIT,
        _pb.ORDER_STATUS_FORWARDING,
        _pb.ORDER_STATUS_SL_FORWARDING,
        _pb.ORDER_STATUS_TP_FORWARDING,
    } if _pb else set()
    _PARTIAL = {_pb.ORDER_STATUS_PARTIALLY_FILLED} if _pb else set()
    _FILLED = {
        _pb.ORDER_STATUS_FILLED,
        _pb.ORDER_STATUS_EXECUTED,
        _pb.ORDER_STATUS_SL_EXECUTED,
        _pb.ORDER_STATUS_TP_EXECUTED,
    } if _pb else set()
    _CANCELLED = {
        _pb.ORDER_STATUS_CANCELED,
        _pb.ORDER_STATUS_REPLACED,
        _pb.ORDER_STATUS_DONE_FOR_DAY,
        _pb.ORDER_STATUS_EXPIRED,
        _pb.ORDER_STATUS_DISABLED,
    } if _pb else set()
    _REJECTED = {
        _pb.ORDER_STATUS_REJECTED,
        _pb.ORDER_STATUS_REJECTED_BY_EXCHANGE,
        _pb.ORDER_STATUS_DENIED_BY_BROKER,
        _pb.ORDER_STATUS_FAILED,
    } if _pb else set()

    if proto_status in _PARTIAL:
        return OrderStatus.PARTIAL_FILL
    if proto_status in _FILLED:
        return OrderStatus.FILLED
    if proto_status in _CANCELLED:
        return OrderStatus.CANCELLED
    if proto_status in _REJECTED:
        return OrderStatus.REJECTED
    log.warning("Order: unknown proto status %s, treating as PENDING", proto_status)
    return OrderStatus.PENDING


class OrderExecutor(IOrderExecutor):
    """Async gRPC order client with risk-first submission.

    Usage::

        executor = OrderExecutor(auth, risk)
        await executor.start()
        local_id = await executor.submit(req)
        await executor.cancel(local_id, account_id)
        await executor.stop()
    """

    def __init__(
        self,
        auth: AuthManager,
        risk: IRiskManager,
        on_update: Optional[OrderUpdateCallback] = None,
    ) -> None:
        self._auth = auth
        self._risk = risk
        self._on_update = on_update
        self._stub = _grpc.OrdersServiceStub(auth.channel) if _grpc else None
        self._account_id = auth.primary_account_id
        self._id_counter = 0
        self._orders: dict[int, _OrderState] = {}  # local_id → state
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._stream_task: Optional[asyncio.Task] = None

    def _metadata(self) -> list[tuple[str, str]]:
        return [("authorization", f"Bearer {self._auth.jwt}")]

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def start(self) -> None:
        """Start the SubscribeOrders background stream."""
        self._stream_task = asyncio.create_task(
            self._order_stream_loop(), name="order-stream"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ #
    # IOrderExecutor interface                                             #
    # ------------------------------------------------------------------ #

    async def submit(self, req: OrderRequest) -> int:
        """Risk-check then place order. Returns local_id.

        Raises:
            FinamError: on validation, risk, or gRPC failure.
        """
        _validate(req)
        await self._risk.check(req)  # raises FinamError on violation

        local_id = self._next_id()
        sym = str(req.symbol)
        log.info(
            "Order submit local=%d sym=%s side=%s type=%s qty=%d price=%g",
            local_id, sym, req.side, req.order_type, req.quantity, req.price,
        )

        grpc_req = _pb.Order()
        grpc_req.account_id = self._account_id
        grpc_req.symbol = sym
        grpc_req.quantity.value = str(req.quantity)
        grpc_req.side = _proto_side(req.side)
        grpc_req.client_order_id = str(local_id)
        if req.order_type == OrderType.MARKET:
            grpc_req.type = _pb.ORDER_TYPE_MARKET
            grpc_req.time_in_force = _pb.TIME_IN_FORCE_IOC
        else:
            grpc_req.type = _pb.ORDER_TYPE_LIMIT
            grpc_req.time_in_force = _pb.TIME_IN_FORCE_DAY
            grpc_req.limit_price.value = f"{req.price:.10g}"

        resp = await self._stub.PlaceOrder(grpc_req, metadata=self._metadata())
        if resp.HasField("reject_reason"):
            raise FinamError(ErrorCode.ORDER_REJECTED, resp.reject_reason)

        async with self._lock:
            self._orders[local_id] = _OrderState(
                exchange_id=resp.order_id,
                symbol=req.symbol,
                side=req.side,
                status=OrderStatus.PENDING,
                order_type=req.order_type,
                price=req.price,
                qty_total=req.quantity,
            )

        log.info("Order placed: local=%d exchange=%s", local_id, resp.order_id)
        return local_id

    async def cancel(self, order_no: int, client_id: str) -> None:  # noqa: ARG002
        """Cancel by local_id. client_id kept for interface compat."""
        async with self._lock:
            state = self._orders.get(order_no)
        if state is None:
            raise FinamError(ErrorCode.INVALID_ARGUMENT,
                             f"order {order_no} not found")
        if not state.exchange_id:
            raise FinamError(ErrorCode.INVALID_ARGUMENT,
                             f"order {order_no} has no exchange id yet")

        req = _pb.CancelOrderRequest(
            account_id=self._account_id,
            order_id=state.exchange_id,
        )
        await self._stub.CancelOrder(req, metadata=self._metadata())
        log.info("Order cancel sent: local=%d exchange=%s", order_no, state.exchange_id)

    # ------------------------------------------------------------------ #
    # Query helpers                                                        #
    # ------------------------------------------------------------------ #

    async def active_orders(self) -> list[_OrderState]:
        async with self._lock:
            return [
                s for s in self._orders.values()
                if s.status in (OrderStatus.PENDING, OrderStatus.PARTIAL_FILL)
            ]

    # ------------------------------------------------------------------ #
    # SubscribeOrders stream                                               #
    # ------------------------------------------------------------------ #

    async def _order_stream_loop(self) -> None:
        log.info("Order stream starting, account=%s", self._account_id)
        async for attempt in exponential_backoff():
            if self._stop.is_set():
                return
            await wait_if_maintenance(self._stop)
            if self._stop.is_set():
                return
            try:
                req = _pb.SubscribeOrdersRequest(account_id=self._account_id)
                async for resp in self._stub.SubscribeOrders(
                    req, metadata=self._metadata()
                ):
                    for order in resp.orders:
                        await self._handle_order_update(order)
            except grpc.aio.AioRpcError as e:
                if self._stop.is_set():
                    return
                log.warning(
                    "Order stream disconnected: %s, retry in %.1fs",
                    e.details(), attempt.delay,
                )
            except asyncio.CancelledError:
                return
        log.info("Order stream stopped")

    async def _handle_order_update(self, order) -> None:  # order: proto OrderState
        try:
            local_id = int(order.order.client_order_id)
        except (ValueError, AttributeError):
            local_id = 0

        try:
            qty_total = int(float(order.initial_quantity.value))
        except (ValueError, AttributeError):
            qty_total = 0

        try:
            qty_filled = int(float(order.executed_quantity.value))
        except (ValueError, AttributeError):
            qty_filled = 0

        status = _map_status(order.status)
        delta_filled = 0

        async with self._lock:
            state = self._orders.get(local_id)
            if state:
                delta_filled = qty_filled - state.qty_filled
                state.status = status
                state.qty_filled = qty_filled
                state.exchange_id = state.exchange_id or order.order_id
                sym = state.symbol
                side = state.side
                price = state.price
            else:
                sym = Symbol(security_code=order.order.symbol, security_board="FORTS")
                from proto.grpc_trade_api.v1 import common_pb2
                side = OrderSide.BUY if order.order.side == common_pb2.SIDE_BUY else OrderSide.SELL
                price = 0.0

        if local_id and self._on_update:
            import datetime
            upd = OrderUpdate(
                order_no=0,
                transaction_id=local_id,
                symbol=sym,
                client_id=self._account_id,
                side=side,
                status=status,
                type=OrderType.MARKET if order.order.type == _pb.ORDER_TYPE_MARKET else OrderType.LIMIT,
                price=price,
                qty_total=qty_total,
                qty_filled=delta_filled,
                message="",
                ts=datetime.datetime.utcnow(),
            )
            self._on_update(upd)


class DryRunExecutor(IOrderExecutor):
    """Drop-in executor that logs but never touches the exchange."""

    def __init__(self) -> None:
        self._id = 0

    async def submit(self, req: OrderRequest) -> int:
        self._id += 1
        log.info(
            "[DRY-RUN] SUBMIT sym=%s side=%s type=%s qty=%d price=%g",
            req.symbol, req.side, req.order_type, req.quantity, req.price,
        )
        return self._id

    async def cancel(self, order_no: int, client_id: str) -> None:
        log.info("[DRY-RUN] CANCEL order_no=%d", order_no)
