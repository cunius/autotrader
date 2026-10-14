"""라이브 페이퍼 트레이딩: 업비트 실시간 체결 + 페이퍼 거래소 (실제 주문 없음)

    python -m autotrader.app.live --symbols KRW-BTC KRW-ETH --minutes 60

구조 (단일 소비자 + 저널):
    [UpbitTradeFeed] ─┐
                      ├─► asyncio.Queue ─► consumer(단일) ─► Runner(Engine + Paper) ─► Journal
    [Clock 1s]       ─┘

- 생산자는 여럿(비동기)이지만 상태를 바꾸는 소비자는 하나 → 락 없이 경쟁 조건 제거
- 도착 순서는 실행마다 다를 수 있지만, 엔진이 소비한 순서를 저널에 남기므로
  `python -m autotrader.app.replay <journal>` 로 언제든 같은 상태를 재현한다
- 무거운 작업(지표 계산 등)은 엔진 밖에서 asyncio.to_thread / 프로세스 풀로
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import time
from datetime import datetime
from pathlib import Path

from ..core.journal import Journal
from ..core.runner import Runner
from ..domain.events import Clock
from ..exchange.upbit_feed import UpbitTradeFeed
from .factory import build_engine, build_paper
from .metrics import Alerter, Metrics

log = logging.getLogger("autotrader")


async def clock(queue: asyncio.Queue, stop: asyncio.Event, interval: float = 1.0) -> None:
    seq = 0
    while not stop.is_set():
        await asyncio.sleep(interval)
        await queue.put(Clock(ts=int(time.time() * 1000), source="clock", seq=seq))
        seq += 1


async def main_async(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    jpath = Path(args.journal_dir) / f"{run_id}.jsonl"
    Path(args.journal_dir).mkdir(parents=True, exist_ok=True)
    (Path(args.journal_dir) / f"{run_id}.config.json").write_text(json.dumps(vars(args)))

    engine = build_engine(args.symbols, initial_cash=args.cash, bar_ms=args.bar_sec * 1000,
                          entry_lookback=args.entry_lookback, exit_lookback=args.exit_lookback)
    metrics, alerter = Metrics(args.metrics_port), Alerter()
    runner = Runner(engine, build_paper(), Journal(jpath),
                    on_processed=lambda e, c: metrics.observe(engine, e, c))

    queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)  # 백프레셔: 소비가 밀리면 생산자 대기
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    feed = UpbitTradeFeed(args.symbols)
    tasks = [asyncio.create_task(feed.run(queue.put, stop)), asyncio.create_task(clock(queue, stop))]
    deadline = time.time() + args.minutes * 60
    log.info("started run=%s journal=%s", run_id, jpath)
    try:
        while not stop.is_set() and time.time() < deadline:
            try:
                e = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            runner.process(e)
            for a in runner.new_alerts():
                metrics.alert(a)
                alerter.send(a)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        runner.journal.close()
        log.info("stopped. equity=%s hash=%s phases=%s", engine.equity(), engine.state_hash()[:16],
                 {s: l.phase.value for s, l in engine.lifecycles.items()})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["KRW-BTC"])
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--cash", default="1000000")
    ap.add_argument("--bar-sec", type=int, default=60)
    ap.add_argument("--entry-lookback", type=int, default=20)
    ap.add_argument("--exit-lookback", type=int, default=10)
    ap.add_argument("--journal-dir", default="journal")
    ap.add_argument("--metrics-port", type=int, default=8000)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
