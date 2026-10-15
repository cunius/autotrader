"""러너: 엔진 ↔ 거래소 사이의 이벤트 루프 (백테스트·페이퍼·라이브 공용)

처리 규칙 (항상 동일):
1) 시장 체결 이벤트가 오면 먼저 거래소가 대기 주문을 매칭 → 체결 이벤트 생성
2) 그다음 엔진이 체결 이벤트들 → 시장 이벤트 순으로 처리
3) 엔진이 낸 커맨드는 즉시 거래소에 전달, 응답 이벤트는 FIFO로 이어서 처리
엔진이 소비한 모든 이벤트는 저널에 기록 → 리플레이로 동일 상태 재현
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable

from ..domain.events import Event, MarketTrade
from .engine import Engine
from .journal import Journal


class Runner:
    def __init__(self, engine: Engine, exchange, journal: Journal | None = None,
                 on_processed: Callable[[Event, list], None] | None = None) -> None:
        self.engine = engine
        self.exchange = exchange
        self.journal = journal
        self.on_processed = on_processed
        self._alerts_seen = 0

    def process(self, e: Event) -> None:
        pending: deque[Event] = deque()
        if isinstance(e, MarketTrade):
            pending.extend(self.exchange.on_market(e))
        else:
            self.exchange.advance(e.ts)
        pending.append(e)
        while pending:
            ev = pending.popleft()
            if self.journal:
                self.journal.append(ev)
            cmds = self.engine.on_event(ev)
            for c in cmds:
                pending.extend(self.exchange.handle(c))
            if self.on_processed:
                self.on_processed(ev, cmds)

    def run(self, events: Iterable[Event]) -> Engine:
        for e in events:
            self.process(e)
        return self.engine

    def new_alerts(self) -> list:
        a = self.engine.alerts[self._alerts_seen:]
        self._alerts_seen = len(self.engine.alerts)
        return a
