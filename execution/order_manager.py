"""주문 관리자: 결정적 client_order_id 발급 + 심볼당 활성 주문 1개 제한

중복 주문 방어선 (바깥 → 안쪽)
1. Lifecycle: ENTERING/EXITING 중엔 새 주문 금지
2. OrderManager: 심볼에 '종결되지 않은' 주문(UNKNOWN 포함)이 있으면 생성 자체를 거부
3. 거래소: 같은 client_order_id 재사용 시 거부 (업비트 identifier, 바이낸스 newClientOrderId)
4. 체결: fill_id 기준 멱등 반영

client_id는 (심볼, 의도, 순번)으로 결정적으로 만든다 → 리플레이해도 같은 ID, 로그 추적이 쉬움
uuid4를 쓰면 재현성이 깨진다
"""
from __future__ import annotations

from decimal import Decimal

from ..domain.order import Order
from ..domain.types import Side


class DuplicateOrderError(Exception):
    pass


class OrderManager:
    def __init__(self, prefix: str = "at") -> None:
        self.prefix = prefix
        self.orders: dict[str, Order] = {}
        self._counter: dict[str, int] = {}
        self._active: dict[str, str] = {}  # symbol → client_id

    def create(self, symbol: str, side: Side, price: Decimal, qty: Decimal,
               intent: str, ts: int) -> Order:
        if symbol in self._active:
            raise DuplicateOrderError(f"{symbol}: 활성 주문 {self._active[symbol]} 존재")
        n = self._counter.get(symbol, 0) + 1
        self._counter[symbol] = n
        cid = f"{self.prefix}-{symbol}-{intent}-{n:06d}"
        order = Order(cid, symbol, side, price, qty, intent, ts)
        self.orders[cid] = order
        self._active[symbol] = cid
        return order

    def get(self, cid: str) -> Order | None:
        return self.orders.get(cid)

    def release(self, order: Order) -> None:
        if order.is_terminal and self._active.get(order.symbol) == order.client_id:
            del self._active[order.symbol]

    def active_orders(self) -> list[Order]:
        return [self.orders[c] for _, c in sorted(self._active.items())]

    def snapshot(self) -> dict:
        return {cid: o.snapshot() for cid, o in sorted(self.orders.items())}
