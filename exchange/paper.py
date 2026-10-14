"""페이퍼 거래소: 실제 돈 없이 주문 흐름을 시뮬레이션 + 장애 주입

- 지정가 주문이 이후 시장 체결가와 교차하면 체결. 체결 가능 수량 = 시장 체결량 × participation
  → participation < 1 이면 자연스럽게 부분 체결이 생긴다
- 장애 주입 (fault_plan 으로 주문별 지정, 또는 확률):
    "reject"   : 접수 거부
    "drop_ack" : 주문은 접수됐는데 ack 메시지가 유실 → 엔진은 타임아웃을 겪는다
    "no_fill"  : 체결 안 됨 → 엔진의 주문 타임아웃/취소 경로
    "lost"     : 요청 자체가 유실 → 조회 시 NOT_FOUND
- 같은 client_id 재사용 시 DUPLICATE_CLIENT_ID 거부 (실거래소와 동일)
- 난수는 seed 고정 → 같은 입력이면 같은 장애 패턴 (재현 가능)
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal

from ..domain.events import (CancelOrder, Command, Event, Fill, MarketTrade, OrderAccepted,
                             OrderCanceled, OrderFilled, OrderRejected, OrderStatusReport,
                             PlaceOrder, QueryOrder)
from ..domain.types import D, D0, Side, floor_to


@dataclass
class PaperConfig:
    fee_rate: Decimal = D("0.0005")
    participation: Decimal = D("1")
    qty_step: Decimal = D("0.00000001")
    reject_rate: float = 0.0
    drop_ack_rate: float = 0.0
    seed: int = 0
    fault_plan: dict[str, str] = field(default_factory=dict)


@dataclass
class _PaperOrder:
    cmd: PlaceOrder
    status: str = "OPEN"
    filled: Decimal = D0
    fills: list[Fill] = field(default_factory=list)
    fault: str | None = None


class PaperExchange:
    source = "paper"

    def __init__(self, cfg: PaperConfig | None = None) -> None:
        self.cfg = cfg or PaperConfig()
        self._rng = random.Random(self.cfg.seed)
        self.orders: dict[str, _PaperOrder] = {}
        self.now = 0
        self._seq = 0

    def _ev(self, cls, **kw) -> Event:
        e = cls(ts=self.now, source=self.source, seq=self._seq, **kw)
        self._seq += 1
        return e

    def advance(self, ts: int) -> None:
        self.now = max(self.now, ts)

    # ------------------------------------------------------- commands
    def handle(self, cmd: Command) -> list[Event]:
        if isinstance(cmd, PlaceOrder):
            return self._place(cmd)
        if isinstance(cmd, CancelOrder):
            return self._cancel(cmd.client_id)
        if isinstance(cmd, QueryOrder):
            return [self._report(cmd.client_id)]
        raise TypeError(cmd)

    def _place(self, cmd: PlaceOrder) -> list[Event]:
        cid = cmd.client_id
        if cid in self.orders:
            return [self._ev(OrderRejected, client_id=cid, reason="DUPLICATE_CLIENT_ID")]
        r_reject, r_drop = self._rng.random(), self._rng.random()  # 항상 2번 뽑아 난수열 고정
        fault = self.cfg.fault_plan.get(cid)
        if fault is None and r_reject < self.cfg.reject_rate:
            fault = "reject"
        elif fault is None and r_drop < self.cfg.drop_ack_rate:
            fault = "drop_ack"
        if fault == "lost":
            return []  # 요청이 거래소에 도달하지 못함 (조회하면 NOT_FOUND)
        if fault == "reject":
            self.orders[cid] = _PaperOrder(cmd, status="REJECTED", fault=fault)
            return [self._ev(OrderRejected, client_id=cid, reason="SIMULATED_REJECT")]
        self.orders[cid] = _PaperOrder(cmd, fault=fault)
        if fault == "drop_ack":
            return []  # 주문은 살아있지만 ack가 사라짐
        return [self._ev(OrderAccepted, client_id=cid)]

    def _cancel(self, cid: str) -> list[Event]:
        po = self.orders.get(cid)
        if po is not None and po.status == "OPEN":
            po.status = "CANCELED"
            return [self._ev(OrderCanceled, client_id=cid)]
        return [self._report(cid)]  # 이미 종결 → 현재 상태로 응답

    def _report(self, cid: str) -> Event:
        po = self.orders.get(cid)
        if po is None:
            return self._ev(OrderStatusReport, client_id=cid, status="NOT_FOUND")
        status = po.status
        if status == "OPEN" and po.filled > 0:
            status = "PARTIALLY_FILLED"
        return self._ev(OrderStatusReport, client_id=cid, status=status, fills=tuple(po.fills))

    # ------------------------------------------------------- matching
    def on_market(self, t: MarketTrade) -> list[Event]:
        self.advance(t.ts)
        out: list[Event] = []
        avail = floor_to(t.qty * self.cfg.participation, self.cfg.qty_step)
        for cid, po in self.orders.items():  # dict 삽입 순서 = 주문 순서 (결정적)
            c = po.cmd
            if po.status != "OPEN" or c.symbol != t.symbol or po.fault == "no_fill":
                continue
            crosses = t.price <= c.price if c.side is Side.BUY else t.price >= c.price
            if not crosses or avail <= 0:
                continue
            q = min(c.qty - po.filled, avail)
            avail -= q
            fee = c.price * q * self.cfg.fee_rate
            fill = Fill(f"{cid}-f{len(po.fills) + 1}", c.price, q, fee)
            po.fills.append(fill)
            po.filled += q
            if po.filled == c.qty:
                po.status = "FILLED"
            out.append(self._ev(OrderFilled, client_id=cid, fill_id=fill.fill_id,
                                price=fill.price, qty=fill.qty, fee=fill.fee))
        return out
