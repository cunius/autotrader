"""백테스트 CLI

    python -m autotrader.app.backtest --synthetic --n 200000
    python -m autotrader.app.backtest --upbit KRW-BTC --candles 2000
"""
from __future__ import annotations

import argparse
import json
import time
from decimal import Decimal

from ..backtest.data import candles_to_trades, synthetic_trades
from ..backtest.report import summarize
from ..core.journal import Journal
from ..core.runner import Runner
from .factory import build_engine, build_paper


def load_upbit(market: str, total: int) -> list:
    from ..exchange.upbit_feed import fetch_minute_candles

    candles, to = [], None
    while len(candles) < total:
        batch = fetch_minute_candles(market, count=min(200, total - len(candles)), to=to)
        if not batch:
            break
        candles += batch
        to = batch[-1]["candle_date_time_utc"].replace("T", " ")
        time.sleep(0.15)  # 요청 제한 준수
    return candles_to_trades(candles, market)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--n", type=int, default=200_000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--upbit", metavar="MARKET")
    ap.add_argument("--candles", type=int, default=2000)
    ap.add_argument("--participation", default="0.5", help="체결량 대비 내 주문 체결 비율 (<1이면 부분체결 발생)")
    ap.add_argument("--journal", default=None)
    args = ap.parse_args()

    if args.upbit:
        symbol, events = args.upbit, load_upbit(args.upbit, args.candles)
    else:
        symbol = "KRW-BTC"
        events = list(synthetic_trades(symbol, args.n, seed=args.seed, vol_bps=6))

    engine = build_engine([symbol])
    paper = build_paper(participation=Decimal(args.participation), seed=args.seed)
    curve = []
    runner = Runner(engine, paper, Journal(args.journal) if args.journal else None)
    for i, e in enumerate(events):
        runner.process(e)
        if i % 60 == 0:
            curve.append(engine.equity())
    print(json.dumps(summarize(engine, curve), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
