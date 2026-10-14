"""이벤트(입력)와 커맨드(출력)

엔진은 이벤트만 입력받고 커맨드만 출력한다. 벽시계(time.time)·난수·네트워크를
엔진 내부에서 직접 쓰지 않으므로, 같은 이벤트 열 → 항상 같은 상태가 된다.

모든 이벤트는 (ts, source, seq)를 가진다.
- ts: 거래소/발생 시각 (ms)
- source: 이벤트를 만든 스트림 이름 (예: "upbit:KRW-BTC", "paper", "clock")
- seq: 해당 source 안에서 0부터 1씩 증가하는 번호 (빈틈 감지·중복 제거용)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from decimal import Decimal

from .types import Side


# ---------------------------------------------------------------- events
@dataclass(frozen=True, kw_only=True)
class Event:
    ts: int
    source: str
    seq: int

    @property
    def order_key(self) -> tuple[int, str, int]:
        """정렬 기준. 도착 순서가 아니라 이 키로 순서를 정한다."""
        return (self.ts, self.source, self.seq)


@dataclass(frozen=True, kw_only=True)
class MarketTrade(Event):
    symbol: str
    price: Decimal
    qty: Decimal
    trade_id: str = ""


@dataclass(frozen=True, kw_only=True)
class Clock(Event):
    """시간 경과만 알리는 이벤트 (타임아웃·쿨다운 판정용)"""


@dataclass(frozen=True, kw_only=True)
class OrderAccepted(Event):
    client_id: str


@dataclass(frozen=True, kw_only=True)
class OrderRejected(Event):
    client_id: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class OrderFilled(Event):
    client_id: str
    fill_id: str
    price: Decimal
    qty: Decimal
    fee: Decimal


@dataclass(frozen=True, kw_only=True)
class OrderCanceled(Event):
    client_id: str


@dataclass(frozen=True)
class Fill:
    fill_id: str
    price: Decimal
    qty: Decimal
    fee: Decimal


@dataclass(frozen=True, kw_only=True)
class OrderStatusReport(Event):
    """주문 조회 결과. 상태가 불확실할 때(UNKNOWN) 거래소를 '진실'로 삼아 맞춘다."""
    client_id: str
    status: str  # OPEN | PARTIALLY_FILLED | FILLED | CANCELED | REJECTED | NOT_FOUND
    fills: tuple[Fill, ...] = ()


# -------------------------------------------------------------- commands
@dataclass(frozen=True)
class PlaceOrder:
    client_id: str
    symbol: str
    side: Side
    price: Decimal  # 지정가 (시장가성 지정가로 슬리피지 상한을 둔다)
    qty: Decimal


@dataclass(frozen=True)
class CancelOrder:
    client_id: str


@dataclass(frozen=True)
class QueryOrder:
    client_id: str


Command = PlaceOrder | CancelOrder | QueryOrder


# --------------------------------------------------------- serialization
_EVENT_TYPES: dict[str, type[Event]] = {
    c.__name__: c
    for c in (MarketTrade, Clock, OrderAccepted, OrderRejected,
              OrderFilled, OrderCanceled, OrderStatusReport)
}
_DECIMAL_FIELDS = {"price", "qty", "fee"}


def _default(o: object) -> object:
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(type(o))


def event_to_json(e: Event) -> str:
    d = asdict(e)
    d["type"] = type(e).__name__
    return json.dumps(d, default=_default, sort_keys=True, ensure_ascii=False)


def event_from_json(s: str) -> Event:
    d = json.loads(s)
    cls = _EVENT_TYPES[d.pop("type")]
    for f in fields(cls):
        if f.name in _DECIMAL_FIELDS:
            d[f.name] = Decimal(d[f.name])
    if cls is OrderStatusReport:
        d["fills"] = tuple(
            Fill(x["fill_id"], Decimal(x["price"]), Decimal(x["qty"]), Decimal(x["fee"]))
            for x in d.get("fills", ())
        )
    return cls(**d)
