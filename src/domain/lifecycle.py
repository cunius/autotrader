"""심볼별 포지션 라이프사이클 상태 머신 (진입/청산/재진입)

  FLAT ──진입주문──► ENTERING ──체결>0──► OPEN ──청산주문──► EXITING ──전량청산──► COOLDOWN ──시간경과──► FLAT
                       │                    ▲                    │
                       └──체결 0(거부/취소)──┘ FLAT로             └──일부만 청산(취소/거부)──► OPEN (청산 재시도)

- 주문이 '살아있는' 동안(ENTERING/EXITING)에는 새 주문을 낼 수 없다 → 중복 주문 1차 방어선.
- 청산 후 바로 재진입하지 않도록 COOLDOWN을 둔다. 쿨다운 길이는 청산 사유별로 다르게(손절은 길게).
- 허용되지 않은 전이는 예외 → 로직 버그를 조용히 넘기지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Phase(str, Enum):
    FLAT = "FLAT"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    COOLDOWN = "COOLDOWN"


P = Phase
_ALLOWED: dict[Phase, frozenset[Phase]] = {
    P.FLAT: frozenset({P.ENTERING}),
    P.ENTERING: frozenset({P.OPEN, P.FLAT}),
    P.OPEN: frozenset({P.EXITING}),
    P.EXITING: frozenset({P.COOLDOWN, P.OPEN}),
    P.COOLDOWN: frozenset({P.FLAT}),
}


class IllegalPhaseTransition(Exception):
    pass


@dataclass
class Lifecycle:
    symbol: str
    phase: Phase = P.FLAT
    active_order: str | None = None
    pending_exit_reason: str | None = None  # 청산이 일부만 됐을 때 재시도용
    cooldown_until: int = 0
    retry_after: int = 0                    # 주문 거부 후 재시도 백오프 (무한 재주문 방지)
    entry_equity_mark: object = None        # 라운드트립 손익 계산용
    history: list[tuple[int, str, str, str]] = field(default_factory=list)

    def to(self, new: Phase, ts: int, reason: str) -> None:
        if new not in _ALLOWED[self.phase]:
            raise IllegalPhaseTransition(f"{self.symbol}: {self.phase.value} → {new.value} ({reason})")
        self.history.append((ts, self.phase.value, new.value, reason))
        self.phase = new

    def force(self, new: Phase, ts: int, reason: str) -> None:
        """정상 경로 밖의 전이. 거래소 상태와 맞추는 '대사(reconcile)' 전용이며 이력에 표시된다."""
        self.history.append((ts, self.phase.value, new.value, f"RECONCILE: {reason}"))
        self.phase = new

    def can_place_order(self, now: int) -> bool:
        return self.active_order is None and self.phase in (P.FLAT, P.OPEN) and now >= self.retry_after

    def snapshot(self) -> dict:
        return {"phase": self.phase.value, "active_order": self.active_order,
                "pending_exit_reason": self.pending_exit_reason,
                "cooldown_until": self.cooldown_until, "retry_after": self.retry_after}
