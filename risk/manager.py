"""리스크 관리: 포지션 사이징, 손절/익절, 최대 낙폭(킬 스위치)"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.position import Position, PriceRef
from ..domain.types import D, D0, floor_to


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: Decimal = D("0.01")     # 1회 손절 시 잃을 자본 비율
    stop_pct: Decimal = D("0.02")
    take_profit_pct: Decimal = D("0.04")
    stop_ref: PriceRef = PriceRef.AVG       # 손절/익절 기준가 (코드 전체에서 이 설정 하나만 사용)
    max_position_pct: Decimal = D("0.30")   # 한 종목 최대 비중
    max_drawdown: Decimal = D("0.10")       # 고점 대비 낙폭 한도 → 신규 진입 중단 + 전량 청산
    qty_step: Decimal = D("0.00000001")
    min_notional: Decimal = D("5000")       # 업비트 KRW 마켓 최소 주문금액
    fee_rate: Decimal = D("0.0005")


class RiskManager:
    def __init__(self, cfg: RiskConfig | None = None) -> None:
        self.cfg = cfg or RiskConfig()

    # ---------------------------------------------------------- sizing
    def size(self, equity: Decimal, cash: Decimal, price: Decimal) -> Decimal:
        """고정 비율 리스크 사이징: 손절 시 손실 = equity × risk_per_trade 가 되도록.

        진입 직후에는 AVG == ENTRY ≈ 주문가이므로 사이징 기준가 = 주문가(price).
        """
        c = self.cfg
        if price <= 0 or equity <= 0:
            return D0
        by_risk = equity * c.risk_per_trade / (price * c.stop_pct)
        by_weight = equity * c.max_position_pct / price
        by_cash = cash / (price * (1 + c.fee_rate))
        qty = floor_to(min(by_risk, by_weight, by_cash), c.qty_step)
        if qty * price < c.min_notional:
            return D0
        return qty

    # ------------------------------------------------------ exit rules
    def stop_price(self, pos: Position) -> Decimal:
        return pos.ref_price(self.cfg.stop_ref) * (1 - self.cfg.stop_pct)

    def take_profit_price(self, pos: Position) -> Decimal:
        return pos.ref_price(self.cfg.stop_ref) * (1 + self.cfg.take_profit_pct)

    def exit_reason(self, pos: Position) -> str | None:
        if pos.is_flat or pos.mark is None:
            return None
        if pos.mark <= self.stop_price(pos):
            return "STOP"
        if pos.mark >= self.take_profit_price(pos):
            return "TAKE_PROFIT"
        return None


class DrawdownGuard:
    """고점 대비 낙폭 감시. 한 번 걸리면 수동 해제 전까지 유지(래치)."""

    def __init__(self, max_drawdown: Decimal, initial_equity: Decimal) -> None:
        self.max_drawdown = max_drawdown
        self.peak = initial_equity
        self.drawdown = D0
        self.tripped = False

    def update(self, equity: Decimal) -> bool:
        """이번 호출에서 새로 발동했으면 True."""
        if equity > self.peak:
            self.peak = equity
        self.drawdown = (self.peak - equity) / self.peak if self.peak > 0 else D0
        if not self.tripped and self.drawdown >= self.max_drawdown:
            self.tripped = True
            return True
        return False

    def snapshot(self) -> dict:
        return {"peak": str(self.peak), "tripped": self.tripped}
