"""이벤트 저널 (append-only JSONL) + 리플레이

엔진이 '실제로 소비한 순서 그대로' 모든 이벤트를 기록한다.
장애 후에는 저널을 다시 엔진에 넣어 상태를 복원하고, 버그 재현도 같은 방식으로 한다.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

from ..domain.events import Event, event_from_json, event_to_json


class Journal:
    def __init__(self, path: str | Path, fsync: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8")
        self._fsync = fsync

    def append(self, e: Event) -> None:
        self._f.write(event_to_json(e) + "\n")
        self._f.flush()
        if self._fsync:
            os.fsync(self._f.fileno())

    def close(self) -> None:
        self._f.close()


def read_journal(path: str | Path) -> Iterator[Event]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield event_from_json(line)


def replay(path: str | Path, engine_factory: Callable[[], object]):
    """저널을 새 엔진에 재생. 커맨드는 버린다 (거래소 응답이 이미 저널에 있으므로)"""
    engine = engine_factory()
    for e in read_journal(path):
        engine.on_event(e)
    return engine
