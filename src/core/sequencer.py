"""시퀀서: 도착 순서와 무관하게 결정적인 처리 순서를 만든다.

여러 스트림(종목별 WebSocket, 거래소 주문 이벤트 등)이 병렬로 들어오면 도착 순서는
네트워크·스레드 스케줄링에 따라 매번 달라진다. 도착 순서대로 처리하면 결과도 매번 달라진다.

해결:
1) 소스별 빈틈 메우기: 각 이벤트는 (source, seq)를 가진다. seq가 연속될 때까지 버퍼링 → 소스 내부 순서 복원.
   이미 처리한 seq가 다시 오면(재연결 등) 중복으로 버린다.
2) 소스 간 k-way 병합: 모든 소스의 '다음 이벤트'가 준비됐을 때만 (ts, source, seq)가 가장 작은 것을 내보낸다.
   (워터마크 = 모든 소스의 다음 이벤트 중 최소) → 어떤 인터리빙으로 도착해도 출력 순서가 같다.
3) 소스가 끝나면 close(source), 전체 종료 시 flush()로 남은 것을 정렬해 방출.

실시간에서 한 소스가 오래 조용하면 병합이 멈춘다 → 하트비트(advance)로 워터마크를 올리거나,
라이브는 '단일 소비자 + 저널' 방식(app/live.py)으로 재현성을 확보한다. (DESIGN.md 참고)
"""
from __future__ import annotations

from collections import deque

from ..domain.events import Event


class Sequencer:
    def __init__(self, sources: list[str]) -> None:
        self._next_seq = {s: 0 for s in sources}
        self._pending: dict[str, dict[int, Event]] = {s: {} for s in sources}
        self._ready: dict[str, deque[Event]] = {s: deque() for s in sources}
        self._closed: set[str] = set()
        self._heartbeat: dict[str, int] = {s: -1 for s in sources}
        self.duplicates = 0

    def push(self, e: Event) -> list[Event]:
        s = e.source
        if s not in self._next_seq:
            raise KeyError(f"등록되지 않은 소스: {s}")
        if e.seq < self._next_seq[s] or e.seq in self._pending[s]:
            self.duplicates += 1
            return []
        self._pending[s][e.seq] = e
        while self._next_seq[s] in self._pending[s]:
            self._ready[s].append(self._pending[s].pop(self._next_seq[s]))
            self._next_seq[s] += 1
        return self._drain()

    def advance(self, source: str, ts: int) -> list[Event]:
        """하트비트: '이 소스에서 ts 이하 이벤트는 더 없다'는 약속."""
        self._heartbeat[source] = max(self._heartbeat[source], ts)
        return self._drain()

    def close(self, source: str) -> list[Event]:
        self._closed.add(source)
        return self._drain()

    def flush(self) -> list[Event]:
        out = self._drain()
        rest = [e for q in self._ready.values() for e in q]
        rest += [e for p in self._pending.values() for e in p.values()]
        for q in self._ready.values():
            q.clear()
        for p in self._pending.values():
            p.clear()
        return out + sorted(rest, key=lambda e: e.order_key)

    def _drain(self) -> list[Event]:
        """k-way 병합. 비어 있는(닫히지 않은) 소스는 '하트비트 시각까지는 더 없음'이라는 자리표시자로 참여"""
        out: list[Event] = []
        while True:
            best_key: tuple | None = None
            best_src: str | None = None
            for s, q in self._ready.items():
                if q:
                    k, cand = q[0].order_key, s
                elif s in self._closed:
                    continue
                else:
                    k, cand = (self._heartbeat[s], "￿", 1 << 62), None
                if best_key is None or k < best_key:
                    best_key, best_src = k, cand
            if best_src is None:  # 모든 소스 소진 or 아직 소식 없는 소스가 가장 이를 수 있음 → 대기
                return out
            out.append(self._ready[best_src].popleft())
