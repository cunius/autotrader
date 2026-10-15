"""업비트 공개 시세 피드 (API 키 불필요, 주문 기능 없음 → 실제 돈이 나갈 수 없음)

- WebSocket: wss://api.upbit.com/websocket/v1, type=trade (실시간 체결)
- REST: /v1/candles/minutes/{unit} (백테스트용 과거 분봉)

재연결 시 같은 체결이 다시 올 수 있으므로 sequential_id로 중복 제거하고,
소스 내부 seq는 수신 순서대로 우리가 매긴다 (WebSocket은 TCP라 한 연결 안에서는 순서 보장)
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from decimal import Decimal

from ..domain.events import MarketTrade

log = logging.getLogger(__name__)

WS_URL = "wss://api.upbit.com/websocket/v1"
REST_URL = "https://api.upbit.com/v1"


def parse_trade(msg: dict, source: str, seq: int) -> MarketTrade:
    # 숫자는 float로 파싱되지 않도록 parse_float=Decimal 로 읽은 dict를 받는다
    return MarketTrade(
        ts=int(msg["trade_timestamp"]), source=source, seq=seq,
        symbol=msg["code"], price=Decimal(msg["trade_price"]), qty=Decimal(msg["trade_volume"]),
        trade_id=str(msg["sequential_id"]),
    )


class UpbitTradeFeed:
    def __init__(self, symbols: list[str], source: str = "upbit",
                 dedupe_window: int = 50_000) -> None:
        self.symbols = symbols
        self.source = source
        self._seq = 0
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._window = dedupe_window
        self.reconnects = 0
        self.duplicates = 0

    def _is_dup(self, trade_id: str) -> bool:
        if trade_id in self._seen:
            self.duplicates += 1
            return True
        self._seen[trade_id] = None
        if len(self._seen) > self._window:
            self._seen.popitem(last=False)
        return False

    async def run(self, emit: Callable[[MarketTrade], Awaitable[None]],
                  stop: asyncio.Event | None = None) -> None:
        import websockets  # 선택 의존성

        backoff = 1.0
        sub = [{"ticket": str(uuid.uuid4())},
               {"type": "trade", "codes": self.symbols, "is_only_realtime": True},
               {"format": "DEFAULT"}]
        while not (stop and stop.is_set()):
            try:
                async with websockets.connect(WS_URL, ping_interval=60) as ws:
                    await ws.send(json.dumps(sub))
                    log.info("upbit ws connected", extra={"symbols": self.symbols})
                    backoff = 1.0
                    async for raw in ws:
                        msg = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
                        if msg.get("type") != "trade" or self._is_dup(str(msg["sequential_id"])):
                            continue
                        await emit(parse_trade(msg, self.source, self._seq))
                        self._seq += 1
                        if stop and stop.is_set():
                            return
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # 네트워크 오류 → 지수 백오프 재연결
                self.reconnects += 1
                log.warning("upbit ws error, reconnecting in %.1fs: %r", backoff, ex)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


def fetch_minute_candles(market: str, unit: int = 1, count: int = 200,
                         to: str | None = None) -> list[dict]:
    """과거 분봉 (최신 → 과거 순으로 옴). 요청 제한(초당 ~10회)에 주의."""
    q = {"market": market, "count": count}
    if to:
        q["to"] = to
    url = f"{REST_URL}/candles/minutes/{unit}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read(), parse_float=Decimal, parse_int=Decimal)
