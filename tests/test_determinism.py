"""[필수 검증] 동시성 및 순서 결정론

처리(도착) 순서가 매번 달라져도 최종 결과(포지션·잔고·로그)가 동일해야 한다.
"""
from __future__ import annotations

import asyncio
import queue
import random
import threading
from decimal import Decimal

from autotrader.app.factory import build_engine, build_paper
from autotrader.backtest.data import synthetic_trades
from autotrader.core.journal import Journal, replay
from autotrader.core.runner import Runner
from autotrader.core.sequencer import Sequencer

SYMS = ["KRW-BTC", "KRW-ETH"]


def _events():
    ev = []
    for i, s in enumerate(SYMS):
        ev += list(synthetic_trades(s, 6_000, seed=i + 1, vol_bps=10, start_price=50_000_000 // (i + 1),
                                    step_ms=1_000 + 7 * i, source=f"feed:{s}"))
    return ev


def _run(ordered_events, paper_kw=None):
    engine = build_engine(SYMS, bar_ms=30_000, entry_lookback=10, exit_lookback=5)
    paper = build_paper(**(paper_kw or {"participation": Decimal("0.3"), "seed": 1}))
    Runner(engine, paper).run(ordered_events)
    return engine


def _sequenced(arrivals):
    seq = Sequencer([f"feed:{s}" for s in SYMS])
    out = []
    for e in arrivals:
        out += seq.push(e)
    return out + seq.flush()


def test_shuffled_arrival_gives_identical_state():
    events = _events()
    reference = _run(sorted(events, key=lambda e: e.order_key))
    assert reference.closed_trades, "테스트 데이터에서 거래가 발생해야 의미가 있음"
    rng = random.Random(42)
    for _ in range(5):
        arrivals = events[:]
        rng.shuffle(arrivals)  # 완전 무작위 도착 (소스 내부 순서까지 뒤섞임)
        assert _run(_sequenced(arrivals)).state_hash() == reference.state_hash()


def test_without_sequencer_results_diverge():
    """대조군: 도착 순서대로 처리하면 결과가 달라진다 → 시퀀서가 필요한 이유"""
    events = _events()
    rng = random.Random(7)
    hashes = set()
    for _ in range(3):
        arrivals = events[:]
        rng.shuffle(arrivals)
        hashes.add(_run(arrivals).state_hash())
    assert len(hashes) > 1


def test_sequencer_drops_duplicates_and_fills_gaps():
    events = sorted(_events(), key=lambda e: e.order_key)[:200]
    seq = Sequencer([f"feed:{s}" for s in SYMS])
    arrivals = events + events[:50]  # 재연결로 50개 중복 수신
    random.Random(3).shuffle(arrivals)
    out = []
    for e in arrivals:
        out += seq.push(e)
    out += seq.flush()
    assert seq.duplicates == 50
    assert out == events


def test_multithreaded_producers_are_deterministic():
    events = _events()
    reference = _run(sorted(events, key=lambda e: e.order_key)).state_hash()
    for run in range(3):
        q: queue.Queue = queue.Queue()
        by_src = {s: [e for e in events if e.source == f"feed:{s}"] for s in SYMS}

        def produce(evts, seed):
            r = random.Random(seed)
            for e in evts:
                if r.random() < 0.01:
                    threading.Event().wait(0.0005)  # 스케줄링 흔들기
                q.put(e)

        threads = [threading.Thread(target=produce, args=(by_src[s], run * 10 + i)) for i, s in enumerate(SYMS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        arrivals = []
        while not q.empty():
            arrivals.append(q.get())
        assert _run(_sequenced(arrivals)).state_hash() == reference


def test_asyncio_producers_are_deterministic():
    events = _events()
    reference = _run(sorted(events, key=lambda e: e.order_key)).state_hash()

    async def scenario(seed):
        q: asyncio.Queue = asyncio.Queue()
        r = random.Random(seed)

        async def produce(evts):
            for i, e in enumerate(evts):
                if i % r.randint(50, 500) == 0:
                    await asyncio.sleep(0)
                await q.put(e)

        await asyncio.gather(*(produce([e for e in events if e.source == f"feed:{s}"]) for s in SYMS))
        return [q.get_nowait() for _ in range(q.qsize())]

    for seed in range(3):
        arrivals = asyncio.run(scenario(seed))
        assert _run(_sequenced(arrivals)).state_hash() == reference


def test_journal_replay_reproduces_state_even_with_faults(tmp_path):
    """장애(거부·ack 유실·부분체결)가 섞인 실행도 저널로 정확히 재현된다."""
    events = sorted(_events(), key=lambda e: e.order_key)
    path = tmp_path / "run.jsonl"
    engine = build_engine(SYMS, bar_ms=30_000, entry_lookback=10, exit_lookback=5)
    paper = build_paper(participation=Decimal("0.2"), reject_rate=0.15, drop_ack_rate=0.15, seed=9)
    j = Journal(path)
    Runner(engine, paper, j).run(events)
    j.close()
    faults = {o.fault for o in paper.orders.values()}
    assert {"reject", "drop_ack"} <= faults  # 장애가 실제로 발생했는지 확인
    replayed = replay(path, lambda: build_engine(SYMS, bar_ms=30_000, entry_lookback=10, exit_lookback=5))
    assert replayed.state_hash() == engine.state_hash()
