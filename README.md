## Deterministic, replayable crypto trading system (Upbit + paper exchange)
수익률보다 같은 입력이면 항상 같은 결과, 장애 상황에서도 상태가 꼬이지 않음을 증명하는 것을 목표로 한 자동매매 시스템
업비트 실시간 시세 (공개 API + 키 불필요) + 페이퍼 거래소(모의 체결)로 동작하므로 실제 돈은 나가지 않음

### 빠른 시작
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[live,dev]"

pytest -q                                              # 28개 테스트
python -m autotrader.app.backtest --synthetic           # 합성 데이터 백테스트 (오프라인)
python -m autotrader.app.backtest --upbit KRW-BTC --candles 3000   # 업비트 분봉 백테스트
python -m autotrader.app.live --symbols KRW-BTC KRW-ETH --minutes 60  # 실시간 페이퍼 트레이딩
python -m autotrader.app.replay journal/<run>.jsonl     # 라이브 실행을 그대로 재현
# 메트릭: http://localhost:8000/metrics (Prometheus 형식)
```

### 구조
```
src/autotrader/
├── domain/            순수 도메인 (I/O 없음)
│   ├── types.py         Decimal 강제 (float 금지)
│   ├── events.py        이벤트(입력)/커맨드(출력) + JSON 직렬화
│   ├── order.py         주문 상태 머신 (PENDING_NEW → … → FILLED/CANCELED/REJECTED, UNKNOWN)
│   ├── lifecycle.py     포지션 라이프사이클 (FLAT → ENTERING → OPEN → EXITING → COOLDOWN)
│   └── position.py      포지션 + PriceRef (ENTRY / AVG / MARK) 단일 계산 지점
├── core/
│   ├── engine.py        결정적 엔진: on_event(event) -> [command]
│   ├── sequencer.py     도착 순서 무관 → 결정적 처리 순서 (빈틈 메우기 + k-way 병합)
│   ├── journal.py       append-only 이벤트 저널 + 리플레이
│   └── runner.py        엔진 ↔ 거래소 루프 (백테스트/라이브 공용)
├── risk/manager.py      사이징, 손절/익절, 최대 낙폭 킬 스위치
├── execution/order_manager.py   결정적 client_order_id, 심볼당 활성 주문 1개
├── exchange/
│   ├── base.py          거래소 어댑터 인터페이스
│   ├── paper.py         모의 체결 + 장애 주입 (거부, ack 유실, 요청 유실, 미체결, 부분체결)
│   └── upbit_feed.py    업비트 WebSocket 체결 피드 + REST 분봉
├── strategy/breakout.py 예시 전략 (돈키언 돌파) — 신호만 냄
├── backtest/            합성/분봉 데이터, 결과 요약
└── app/                 CLI: backtest, live, replay, metrics
```
