"""백테스트 데이터: 합성 체결(시드 고정) / 업비트 분봉 → 체결 변환"""
from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal

from ..domain.events import MarketTrade
from ..domain.types import D


def synthetic_trades(symbol: str, n: int, start_ts: int = 1_700_000_000_000,
                     start_price: int = 50_000_000, step_ms: int = 1_000,
                     vol_bps: int = 8, drift_bps: float = 0.0, seed: int = 0,
                     source: str | None = None) -> Iterator[MarketTrade]:
    """랜덤 워크 체결. 가격 변화는 정수 bps로 만들어 float 오염 없이 Decimal로 변환"""
    rng = random.Random(seed)
    price = D(start_price)
    src = source or f"syn:{symbol}"
    for i in range(n):
        bps = round(rng.gauss(drift_bps, vol_bps))
        price = max(D(1), (price * (D(10_000) + D(bps)) / D(10_000)).quantize(D(1)))
        qty = D(rng.randint(1, 50)) / D(1000)
        yield MarketTrade(ts=start_ts + i * step_ms, source=src, seq=i,
                          symbol=symbol, price=price, qty=qty, trade_id=f"{symbol}-{i}")


def candles_to_trades(candles: list[dict], symbol: str, source: str = "candles") -> list[MarketTrade]:
    """분봉 OHLC → 봉당 4개의 가상 체결

    봉 안에서 고가/저가 중 무엇이 먼저였는지 모르므로, 롱 전략에 '불리한' 경로를 가정한다:
    양봉 O→L→H→C (손절이 먼저 닿게), 음봉 O→H→L→C. 낙관적 가정은 백테스트를 부풀린다
    """
    out: list[MarketTrade] = []
    rows = sorted(candles, key=lambda c: c["candle_date_time_utc"])
    seq = 0
    for c in rows:
        t0 = int(datetime.fromisoformat(c["candle_date_time_utc"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
        o, h, l, cl = (Decimal(str(c[k])) for k in ("opening_price", "high_price", "low_price", "trade_price"))
        vol = Decimal(str(c["candle_acc_trade_volume"])) / 4
        path = (o, l, h, cl) if cl >= o else (o, h, l, cl)
        for j, p in enumerate(path):
            out.append(MarketTrade(ts=t0 + j * 15_000, source=source, seq=seq, symbol=symbol,
                                   price=p, qty=vol, trade_id=f"{symbol}-{t0}-{j}"))
            seq += 1
    return out
