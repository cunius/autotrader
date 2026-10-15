"""포지션과 가격 기준점(PriceRef)

"손절 -2%"의 기준이 진입가인지, 평균단가인지, 현재가인지가 코드 곳곳에서 다르면
부분 체결/분할 매수 시 손절 라인이 모듈마다 달라진다.
→ 기준가는 Position.ref_price(PriceRef) 한 곳에서만 계산하고, 설정으로 어떤 기준을 쓸지 명시한다.

- ENTRY: 이번 포지션의 '첫 체결가'
- AVG:   보유 수량의 평균단가 (이동평균법, 수수료 제외)
- MARK:  최신 시장가
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .types import D0, Side


class PriceRef(str, Enum):
    ENTRY = "ENTRY"
    AVG = "AVG"
    MARK = "MARK"


class PositionError(Exception):
    pass


@dataclass
class Position:
    symbol: str
    qty: Decimal = D0
    cost: Decimal = D0            # 보유분 원가 합계 (평균단가 × 수량)
    entry_price: Decimal | None = None
    mark: Decimal | None = None
    realized_pnl: Decimal = D0    # 수수료 제외 실현손익
    fees: Decimal = D0

    @property
    def avg_price(self) -> Decimal | None:
        return self.cost / self.qty if self.qty else None

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    def ref_price(self, ref: PriceRef) -> Decimal:
        """가격 기준점의 단일 진입점. 다른 모듈은 이 함수만 사용한다."""
        if ref is PriceRef.ENTRY:
            v = self.entry_price
        elif ref is PriceRef.AVG:
            v = self.avg_price
        else:
            v = self.mark
        if v is None:
            raise PositionError(f"{self.symbol}: {ref.value} 기준가 없음 (qty={self.qty})")
        return v

    def unrealized_pnl(self) -> Decimal:
        if self.is_flat or self.mark is None:
            return D0
        return self.mark * self.qty - self.cost

    def on_fill(self, side: Side, price: Decimal, qty: Decimal, fee: Decimal) -> Decimal:
        """체결 반영. 이번 체결의 실현손익을 반환 (매수는 0)."""
        self.fees += fee
        if side is Side.BUY:
            if self.is_flat:
                self.entry_price = price
            self.qty += qty
            self.cost += price * qty
            return D0
        if qty > self.qty:
            raise PositionError(f"{self.symbol}: 매도 {qty} > 보유 {self.qty} (현물 공매도 불가)")
        if qty == self.qty:
            released = self.cost  # 전량 청산 시 나눗셈 잔차 없이 0으로
        else:
            released = self.cost * qty / self.qty
        pnl = price * qty - released
        self.realized_pnl += pnl
        self.qty -= qty
        self.cost -= released
        if self.is_flat:
            self.cost = D0
            self.entry_price = None
        return pnl

    def snapshot(self) -> dict:
        return {"qty": str(self.qty), "cost": str(self.cost),
                "entry_price": str(self.entry_price), "realized_pnl": str(self.realized_pnl),
                "fees": str(self.fees)}
