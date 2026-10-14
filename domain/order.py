"""주문 상태 머신

                 ┌──────────── REJECTED
                 │
   PENDING_NEW ──┼──► OPEN ──► PARTIALLY_FILLED ──► FILLED
        │        │      │              │
        │        │      └──► PENDING_CANCEL ──► CANCELED
        └── (ack 타임아웃) ──► UNKNOWN ──(조회 결과로 복구)──► 어느 상태든

핵심 원칙
- 타임아웃 ≠ 실패. ack가 안 왔다고 재주문하면 이중 주문이 된다.
  → UNKNOWN으로 두고 조회(QueryOrder)로 실제 상태를 확인한다.
- 체결은 '사실'이다. 같은 fill_id는 한 번만 반영(멱등), 취소 후 늦게 온 체결도 장부에는 반영한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .types import D0, Side


class OrderStatus(str, Enum):
    PENDING_NEW = "PENDING_NEW"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    UNKNOWN = "UNKNOWN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


S = OrderStatus
TERMINAL = frozenset({S.FILLED, S.CANCELED, S.REJECTED})

_ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    # 체결이 ack보다 먼저 도착할 수 있으므로 PENDING_NEW → 체결 상태 허용
    S.PENDING_NEW: frozenset({S.OPEN, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED,
                              S.UNKNOWN, S.PENDING_CANCEL, S.CANCELED}),
    S.OPEN: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.PENDING_CANCEL, S.CANCELED, S.UNKNOWN}),
    S.PARTIALLY_FILLED: frozenset({S.FILLED, S.PENDING_CANCEL, S.CANCELED, S.UNKNOWN}),
    S.PENDING_CANCEL: frozenset({S.FILLED, S.CANCELED, S.UNKNOWN}),
    S.UNKNOWN: frozenset({S.OPEN, S.PARTIALLY_FILLED, S.PENDING_CANCEL,
                          S.FILLED, S.CANCELED, S.REJECTED}),
    S.FILLED: frozenset(),
    S.CANCELED: frozenset(),
    S.REJECTED: frozenset(),
}


class InvalidTransition(Exception):
    pass


class OverfillError(Exception):
    pass


@dataclass
class Order:
    client_id: str
    symbol: str
    side: Side
    price: Decimal
    qty: Decimal
    intent: str  # "ENTRY" | "EXIT"
    created_ts: int
    status: OrderStatus = S.PENDING_NEW
    status_ts: int = 0
    filled_qty: Decimal = D0
    filled_notional: Decimal = D0
    fees: Decimal = D0
    fill_ids: set[str] = field(default_factory=set)
    history: list[tuple[int, str, str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status_ts = self.created_ts

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def remaining(self) -> Decimal:
        return self.qty - self.filled_qty

    @property
    def avg_fill_price(self) -> Decimal | None:
        return self.filled_notional / self.filled_qty if self.filled_qty else None

    def transition(self, new: OrderStatus, ts: int, reason: str = "") -> None:
        if new == self.status:
            return  # 중복 ack 등은 멱등 처리
        if new not in _ALLOWED[self.status]:
            raise InvalidTransition(f"{self.client_id}: {self.status.value} → {new.value} ({reason})")
        self.history.append((ts, self.status.value, new.value, reason))
        self.status = new
        self.status_ts = ts

    def apply_fill(self, fill_id: str, price: Decimal, qty: Decimal, fee: Decimal, ts: int) -> bool:
        """체결 반영. 이미 반영된 fill_id면 False (중복 무시)."""
        if fill_id in self.fill_ids:
            return False
        if self.filled_qty + qty > self.qty:
            raise OverfillError(f"{self.client_id}: {self.filled_qty}+{qty} > {self.qty}")
        self.fill_ids.add(fill_id)
        self.filled_qty += qty
        self.filled_notional += price * qty
        self.fees += fee
        if self.is_terminal:
            return True  # 취소 후 늦게 온 체결: 장부엔 반영, 상태는 유지 (엔진이 경보)
        if self.filled_qty == self.qty:
            self.transition(S.FILLED, ts, "fill")
        elif self.status in (S.PENDING_NEW, S.OPEN, S.UNKNOWN):
            self.transition(S.PARTIALLY_FILLED, ts, "fill")
        return True

    def snapshot(self) -> dict:
        return {"status": self.status.value, "filled_qty": str(self.filled_qty),
                "filled_notional": str(self.filled_notional), "fees": str(self.fees),
                "qty": str(self.qty), "price": str(self.price), "side": self.side.value}
