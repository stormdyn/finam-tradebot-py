"""src/api/order_client.py — клиент OrdersService (аналог C++ OrderClient).

Отвечает за:
  - submit / cancel (unary PlaceOrder / CancelOrder)
  - SubscribeOrders stream → on_update callback
  - Маппинг local_id ↔ exchange_id
  - Risk-first: check() перед каждым submit()
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import grpc
import grpc.aio

from core.domain import (
    OrderRequest, OrderSide, OrderStatus, OrderType, OrderUpdate, Symbol,
)
from core.errors import AppError, ErrorCode
from core.backoff import exponential_backoff
from core.maintenance import wait_if_maintenance
from core.interfaces import IOrderExecutor, IRiskManager
from auth.manager import AuthManager

logger = logging.getLogger(__name__)

OrderUpdateCallback = Callable[[OrderUpdate], None]


@dataclass
class _OrderState:
    exchange_id: str = ""
    symbol: Symbol = field(default_factory=lambda: Symbol("UNKNOWN"))
    side: OrderSide = OrderSide.BUY
    status: OrderStatus = OrderStatus.PENDING
    order_type: OrderType = OrderType.MARKET
    price: float = 0.0
    qty_total: int = 0
    qty_filled: int = 0


def _validate(req: OrderRequest) -> None:
    if not req.client_id:
        raise AppError(ErrorCode.INVALID_ARGUMENT, "client_id is empty")
    if req.quantity <= 0:
        raise AppError(ErrorCode.INVALID_QUANTITY, f"quantity must be > 0, got {req.quantity}")
    if req.order_type == OrderType.LIMIT and req.price <= 0.0:
        raise AppError(ErrorCode.INVALID_PRICE, "limit order requires price > 0")


# Маппинг proto OrderStatus → domain OrderStatus
def _map_proto_status(proto_status: int) -> OrderStatus:
    """
    Числовые значения соответствуют ordersservice.proto.
    Поддерживаем все ~20 промежуточных статусов как в C++ mapstatus().
    """
    # Значения заглушки — заменятся реальными константами после генерации stubs
    _PARTIAL  = {15}
    _FILLED   = {16, 17, 18, 19}
    _CANCELLED = {20, 21, 22, 23, 24}
    _REJECTED  = {25, 26, 27, 28}

    if proto_status in _PARTIAL:
        return OrderStatus.PARTIAL_FILL
    if proto_status in _FILLED:
        return OrderStatus.FILLED
    if proto_status in _CANCELLED:
        return OrderStatus.CANCELLED
    if proto_status in _REJECTED:
        return OrderStatus.REJECTED
    return OrderStatus.PENDING


class OrderExecutor(IOrderExecutor):
    """Async gRPC order client с risk-first submission."""

    def __init__(
        self,
        auth: AuthManager,
        risk: IRiskManager,
        on_update: Optional[OrderUpdateCallback] = None,
    ) -> None:
        self._auth = auth
        self._risk = risk
        self._on_update = on_update
        self._account_id = auth.primary_account_id
        self._id_counter = 0
        self._orders: dict[int, _OrderState] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._stream_task: Optional[asyncio.Task] = None
        self._stub = None

    def _get_stub(self):
        if self._stub is None:
            try:
                from proto.grpc_trade_api.v1.orders import ordersservice_pb2_grpc  # type: ignore
                self._stub = ordersservice_pb2_grpc.OrdersServiceStub(self._auth.channel)
            except ModuleNotFoundError as exc:
                raise AppError(ErrorCode.INTERNAL, "Proto stubs not found. Run: make proto") from exc
        return self._stub

    def _metadata(self) -> list[tuple[str, str]]:
        return self._auth.metadata()

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def start(self) -> None:
        """Запустить фоновый SubscribeOrders stream."""
        self._stream_task = asyncio.create_task(
            self._order_stream_loop(), name="order-stream"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ #
    # IOrderExecutor                                                       #
    # ------------------------------------------------------------------ #

    async def submit(self, req: OrderRequest) -> int:
        """
        Risk-check → PlaceOrder.

        Raises:
            AppError: при нарушении риска, валидации или ошибке gRPC.
        """
        _validate(req)
        await self._risk.check(req)  # raises AppError on violation

        local_id = self._next_id()
        logger.info(
            "[Order] submit local=%d sym=%s side=%s type=%s qty=%d price=%g",
            local_id, req.symbol, req.side.value,
            req.order_type.value, req.quantity, req.price,
        )

        try:
            from proto.grpc_trade_api.v1.orders import ordersservice_pb2 as pb  # type: ignore
            from proto.grpc_trade_api.v1 import common_pb2  # type: ignore
        except ModuleNotFoundError as exc:
            raise AppError(ErrorCode.INTERNAL, "Proto stubs not found") from exc

        grpc_req = pb.Order()
        grpc_req.account_id = self._account_id
        grpc_req.symbol = str(req.symbol)
        grpc_req.quantity.value = str(req.quantity)
        grpc_req.side = common_pb2.SIDE_BUY if req.side == OrderSide.BUY else common_pb2.SIDE_SELL
        grpc_req.client_order_id = str(local_id)

        if req.order_type == OrderType.MARKET:
            grpc_req.type = pb.ORDER_TYPE_MARKET
            grpc_req.time_in_force = pb.TIME_IN_FORCE_IOC
        else:
            grpc_req.type = pb.ORDER_TYPE_LIMIT
            grpc_req.time_in_force = pb.TIME_IN_FORCE_DAY
            grpc_req.limit_price.value = f"{req.price:.10g}"

        try:
            resp = await self._get_stub().PlaceOrder(grpc_req, metadata=self._metadata())
        except grpc.aio.AioRpcError as e:
            raise AppError(ErrorCode.RPC_ERROR, f"PlaceOrder failed: {e.details()}") from e

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

        logger.info("[Order] placed: local=%d exchange=%s", local_id, resp.order_id)
        return local_id

    async def cancel(self, order_no: int, client_id: str) -> None:  # noqa: ARG002
        async with self._lock:
            state = self._orders.get(order_no)
        if state is None:
            raise AppError(ErrorCode.INVALID_ARGUMENT, f"order {order_no} not found")
        if not state.exchange_id:
            raise AppError(ErrorCode.INVALID_ARGUMENT, f"order {order_no} has no exchange id yet")

        try:
            from proto.grpc_trade_api.v1.orders import ordersservice_pb2 as pb  # type: ignore
        except ModuleNotFoundError as exc:
            raise AppError(ErrorCode.INTERNAL, "Proto stubs not found") from exc

        req = pb.CancelOrderRequest(
            account_id=self._account_id,
            order_id=state.exchange_id,
        )
        try:
            await self._get_stub().CancelOrder(req, metadata=self._metadata())
        except grpc.aio.AioRpcError as e:
            raise AppError(ErrorCode.RPC_ERROR, f"CancelOrder failed: {e.details()}") from e

        logger.info("[Order] cancel sent: local=%d exchange=%s", order_no, state.exchange_id)

    # ------------------------------------------------------------------ #
    # SubscribeOrders stream                                               #
    # ------------------------------------------------------------------ #

    async def _order_stream_loop(self) -> None:
        logger.info("[Order] stream starting, account=%s", self._account_id)
        try:
            from proto.grpc_trade_api.v1.orders import ordersservice_pb2 as pb  # type: ignore
            from proto.grpc_trade_api.v1 import common_pb2  # type: ignore
        except ModuleNotFoundError as exc:
            raise AppError(ErrorCode.INTERNAL, "Proto stubs not found") from exc

        async for attempt in exponential_backoff():
            if self._stop.is_set():
                return
            await wait_if_maintenance(self._stop)
            if self._stop.is_set():
                return
            try:
                req = pb.SubscribeOrdersRequest(account_id=self._account_id)
                async for resp in self._get_stub().SubscribeOrders(req, metadata=self._metadata()):
                    for order in resp.orders:
                        await self._handle_order_update(order, common_pb2)
            except grpc.aio.AioRpcError as e:
                if self._stop.is_set():
                    return
                logger.warning(
                    "[Order] stream disconnected: %s, retry in %.1fs",
                    e.details(), attempt.delay,
                )
            except asyncio.CancelledError:
                return
        logger.info("[Order] stream stopped")

    async def _handle_order_update(self, order, common_pb2) -> None:
        try:
            local_id = int(order.order.client_order_id)
        except (ValueError, AttributeError):
            return  # неизвестный ордер — игнорируем

        try:
            qty_total = int(float(order.initial_quantity.value))
        except (ValueError, AttributeError):
            qty_total = 0
        try:
            qty_filled = int(float(order.executed_quantity.value))
        except (ValueError, AttributeError):
            qty_filled = 0

        status = _map_proto_status(order.status)

        async with self._lock:
            state = self._orders.get(local_id)
            if state is None:
                return
            prev_filled = state.qty_filled
            state.status = status
            state.qty_filled = qty_filled
            if not state.exchange_id:
                state.exchange_id = getattr(order, "order_id", "")
            delta_filled = qty_filled - prev_filled
            sym, side, price = state.symbol, state.side, state.price

        if self._on_update and delta_filled >= 0:
            upd = OrderUpdate(
                order_no=0,
                transaction_id=local_id,
                symbol=sym,
                client_id=self._account_id,
                side=side,
                status=status,
                type=OrderType.MARKET if order.order.type == 1 else OrderType.LIMIT,
                price=price,
                qty_total=qty_total,
                qty_filled=delta_filled,
                message="",
                ts=datetime.utcnow(),
            )
            self._on_update(upd)


class DryRunExecutor(IOrderExecutor):
    """Executor, который логирует ордера, но не отправляет их на биржу."""

    def __init__(self) -> None:
        self._id = 0

    async def submit(self, req: OrderRequest) -> int:
        self._id += 1
        logger.info(
            "[DRY-RUN] SUBMIT sym=%s side=%s type=%s qty=%d price=%g",
            req.symbol, req.side.value, req.order_type.value, req.quantity, req.price,
        )
        return self._id

    async def cancel(self, order_no: int, client_id: str) -> None:
        logger.info("[DRY-RUN] CANCEL order_no=%d", order_no)
