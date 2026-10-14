"""공통 타입. 금액/수량은 전부 Decimal — float는 상태에 들어오지 못하게 막는다.

float를 쓰면 0.1 + 0.2 != 0.3 같은 오차 때문에 '같은 입력 → 같은 출력'이 깨지고,
잔고/포지션 비교가 플랫폼·연산 순서에 따라 달라질 수 있다.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal, getcontext
from enum import Enum

getcontext().prec = 34  # IEEE 754 decimal128 수준

D0 = Decimal(0)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


def D(x: object) -> Decimal:
    """Decimal 변환. float는 의도적으로 거부한다."""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        raise TypeError(f"float 금지 (값={x!r}): str 또는 int로 전달하세요")
    return Decimal(str(x))


def floor_to(x: Decimal, step: Decimal) -> Decimal:
    """수량: 항상 내림 (보유량/잔고를 초과하지 않도록)"""
    return (x / step).to_integral_value(ROUND_DOWN) * step


def ceil_to(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(ROUND_UP) * step
