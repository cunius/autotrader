## Deterministic, replayable crypto trading system (Upbit + paper exchange)
수익률보다 같은 입력이면 항상 같은 결과, 장애 상황에서도 상태가 꼬이지 않음을 증명하는 것을 목표로 한 자동매매 시스템
업비트 실시간 시세 (공개 API + 키 불필요) + 페이퍼 거래소(모의 체결)로 동작하므로 실제 돈은 나가지 않음

### 시작
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

|---|---|---|
| **동시성·순서 결정론** | `Sequencer`(소스별 seq 빈틈 메우기 + (ts, source, seq) k-way 병합), 엔진은 벽시계·난수 미사용, 상태 해시 | `test_determinism.py`: 무작위 셔플·멀티스레드·asyncio 생산자 모두 동일 해시, 시퀀서 없으면 결과가 달라지는 대조군, 장애 섞인 실행의 저널 리플레이 |
| **재진입/청산 상태 관리** | `Lifecycle` 상태 머신 + 청산 사유별 쿨다운, 불법 전이는 예외 | `test_edge_cases.py`: 부분 체결 진입, 부분 청산 후 잔량 재시도, 손절 후 30분 재진입 금지 |
| **엣지 케이스** (부분 체결·타임아웃) | `Order` 상태 머신의 UNKNOWN 상태, 타임아웃 → 조회(재주문 X), 미체결 잔량 자동 취소 | ack 유실, 요청 유실(NOT_FOUND), 거부 백오프, 늦은 체결 |
| **가격 기준점 명시** | `PriceRef` + `Position.ref_price()` 단일 진입점, `RiskConfig.stop_ref`로 기준 선택 | 분할 체결 시 ENTRY와 AVG 기준 손절 판단이 달라지는 파라미터 테스트, 사이징 = 자본의 1% 리스크 |
| **중복·오류 주문 방지** | 4중 방어: 라이프사이클 → OrderManager → 거래소 client_id → fill_id 멱등 | 중복 체결 무시, 활성/UNKNOWN 주문 존재 시 생성 거부 |
| 리스크 관리 | 고정 비율 사이징, 비중 상한, 최소 주문금액, 최대 낙폭 래치 | 낙폭 초과 → 전량 청산 + 신규 진입 중단 |
| 실시간 파이프라인 | asyncio 생산자 여럿 + 단일 소비자, 백프레셔 큐, 재연결 지수 백오프, 체결 ID 중복 제거 | 업비트 메시지 파싱 테스트 |
| 모니터링·알림 | Prometheus 메트릭(처리량, 피드 지연, 낙폭, 알림), CRITICAL은 Slack 웹훅 | |

자세한 설계 근거는 [`docs/DESIGN.md`](docs/DESIGN.md), 다음 단계는 [`docs/ROADMAP.md`](docs/ROADMAP.md) 참고

