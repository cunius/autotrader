"""모니터링: Prometheus 메트릭 + 알림

prometheus_client가 없으면 아무것도 하지 않는 Null 구현으로 대체된다.
Grafana에서 볼 핵심 지표:
- autotrader_events_total{type}         이벤트 처리량
- autotrader_orders_total{action}       주문/취소/조회 커맨드 수
- autotrader_alerts_total{level,code}   알림 (CRITICAL은 즉시 통보)
- autotrader_equity / drawdown          자산·낙폭
- autotrader_feed_lag_ms                거래소 체결시각 대비 수신 지연 (피드 이상 감지)
- autotrader_last_event_age_seconds     마지막 시세 이후 경과 (피드 끊김 감지)
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

log = logging.getLogger("autotrader")

try:
    from prometheus_client import Counter, Gauge, start_http_server
    _PROM = True
except ImportError:  # pragma: no cover
    _PROM = False


class Metrics:
    def __init__(self, port: int | None = 8000) -> None:
        self.enabled = _PROM and port is not None
        if not self.enabled:
            return
        start_http_server(port)
        self.events = Counter("autotrader_events_total", "events", ["type"])
        self.orders = Counter("autotrader_orders_total", "commands", ["action"])
        self.alerts = Counter("autotrader_alerts_total", "alerts", ["level", "code"])
        self.equity = Gauge("autotrader_equity", "equity KRW")
        self.drawdown = Gauge("autotrader_drawdown", "drawdown ratio")
        self.feed_lag = Gauge("autotrader_feed_lag_ms", "exchange ts → receive")
        self.last_event = Gauge("autotrader_last_event_unixtime", "last market event")

    def observe(self, engine, event, cmds) -> None:
        if not self.enabled:
            return
        self.events.labels(type(event).__name__).inc()
        for c in cmds:
            self.orders.labels(type(c).__name__).inc()
        if type(event).__name__ == "MarketTrade":
            self.feed_lag.set(time.time() * 1000 - event.ts)
            self.last_event.set(time.time())
            self.equity.set(float(engine.equity()))   # 메트릭 출력용으로만 float
            self.drawdown.set(float(engine.dd.drawdown))

    def alert(self, a) -> None:
        if self.enabled:
            self.alerts.labels(a.level, a.code).inc()


class Alerter:
    """로그 + (선택) 웹훅. SLACK_WEBHOOK_URL 환경변수가 있으면 CRITICAL을 전송"""

    def __init__(self) -> None:
        self.webhook = os.environ.get("SLACK_WEBHOOK_URL")

    def send(self, a) -> None:
        line = {"ts": a.ts, "level": a.level, "code": a.code, "msg": a.msg}
        (log.error if a.level == "CRITICAL" else log.warning)(json.dumps(line, ensure_ascii=False))
        if self.webhook and a.level == "CRITICAL":
            try:
                body = json.dumps({"text": f"[{a.level}] {a.code}: {a.msg}"}).encode()
                req = urllib.request.Request(self.webhook, body, {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3)
            except Exception as ex:  # 알림 실패가 매매 루프를 죽이면 안 된다
                log.error("webhook failed: %r", ex)
