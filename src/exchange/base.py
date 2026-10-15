"""거래소 어댑터 인터페이스. 페이퍼/바이낸스 테스트넷/업비트 실거래 어댑터가 모두 이것을 구현한다.

엔진은 이 인터페이스만 알기 때문에 어댑터를 바꿔도 전략·리스크·상태 머신 코드는 그대로다.
"""
from __future__ import annotations

from typing import Protocol

from ..domain.events import Command, Event, MarketTrade


class Exchange(Protocol):
    source: str

    def handle(self, cmd: Command) -> list[Event]:
        """주문/취소/조회 커맨드 처리. 동기 응답 이벤트를 반환 (비동기 거래소는 [] 반환 후 큐로 전달)."""

    def on_market(self, t: MarketTrade) -> list[Event]:
        """시장 이벤트 통지. 페이퍼는 여기서 매칭, 실거래 어댑터는 [] 반환."""

    def advance(self, ts: int) -> None:
        ...
