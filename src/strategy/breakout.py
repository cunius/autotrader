"""예시 전략: 체결 → 분봉 집계 → 돈키언 채널 돌파

전략은 '신호'만 낸다. 수량·손절·주문 여부는 엔진/리스크가 결정한다 (책임 분리)
봉 경계는 ts // bar_ms 로만 정하므로 벽시계와 무관하게 결정적이다
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Signal(str, Enum):
    ENTER = "ENTER"
    EXIT = "EXIT"


@dataclass
class Bar:
    start: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def update(self, price: Decimal) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price


class BreakoutStrategy:
    def __init__(self, bar_ms: int = 60_000, entry_lookback: int = 20, exit_lookback: int = 10) -> None:
        self.bar_ms = bar_ms
        self.entry_lookback = entry_lookback
        self.exit_lookback = exit_lookback
        self._cur: dict[str, Bar] = {}
        self._bars: dict[str, deque[Bar]] = {}
        self.late_trades = 0

    def on_trade(self, symbol: str, ts: int, price: Decimal) -> Signal | None:
        start = ts - ts % self.bar_ms
        cur = self._cur.get(symbol)
        if cur is None:
            self._cur[symbol] = Bar(start, price, price, price, price)
            self._bars[symbol] = deque(maxlen=max(self.entry_lookback, self.exit_lookback))
            return None
        if start < cur.start:
            self.late_trades += 1  # 이미 닫힌 봉의 체결: 무시 (시퀀서가 정상이면 0이어야 함)
            return None
        if start == cur.start:
            cur.update(price)
            return None
        sig = self._evaluate(symbol, cur)
        self._bars[symbol].append(cur)
        self._cur[symbol] = Bar(start, price, price, price, price)
        return sig

    def _evaluate(self, symbol: str, bar: Bar) -> Signal | None:
        hist = list(self._bars[symbol])
        if len(hist) >= self.entry_lookback:
            if bar.close > max(b.high for b in hist[-self.entry_lookback:]):
                return Signal.ENTER
        if len(hist) >= self.exit_lookback:
            if bar.close < min(b.low for b in hist[-self.exit_lookback:]):
                return Signal.EXIT
        return None

    def snapshot(self) -> dict:
        return {s: {"bars": len(self._bars[s]), "cur_start": b.start, "cur_close": str(b.close)}
                for s, b in sorted(self._cur.items())}
