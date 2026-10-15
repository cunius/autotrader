"""[필수 검증] 재진입/청산 상태 관리 + 중복·오류 주문 방지

부분 체결, 주문 타임아웃, ack 유실, 요청 유실, 거부, 중복 체결, 늦은 체결, 킬 스위치
"""
from __future__ import annotations

import pytest

from autotrader.domain.events import OrderCanceled, OrderFilled
from autotrader.domain.lifecycle import Phase
from autotrader.domain.order import OrderStatus
from autotrader.domain.types import D, Side
from autotrader.execution.order_manager import DuplicateOrderError, OrderManager
from autotrader.risk.manager import RiskConfig
from autotrader.strategy.breakout import Signal

from conftest import SYM

E, X = Signal.ENTER, Signal.EXIT
CID1 = f"at-{SYM}-ENTRY-000001"
FULL = D("0.00599400")  # 자본 100만, 비중 30%, 지정가 50,050,000 기준 수량


def test_partial_entry_then_timeout_opens_with_partial_qty(make):
    h = make({0: E, 11_000: X})
    h.trade(0, 50_000_000)                       # 신호 → 매수 지정가 50,050,000
    assert h.lc.phase is Phase.ENTERING
    h.trade(1_000, 50_000_000, qty="0.002")      # 0.002만 체결 (부분 체결)
    assert h.engine.om.get(CID1).status is OrderStatus.PARTIALLY_FILLED
    for ts in range(2_000, 10_001, 1_000):       # 지정가보다 높아 더 이상 체결 안 됨
        h.trade(ts, 50_100_000)
    order = h.engine.om.get(CID1)
    assert order.status is OrderStatus.CANCELED   # 10초 타임아웃 → 잔량 취소
    assert h.lc.phase is Phase.OPEN and h.pos.qty == D("0.002")
    h.trade(11_000, 50_100_000)                  # 청산 신호 → 매도 수량은 '실제 보유량'
    assert h.placed[-1].qty == D("0.002")
    h.trade(12_000, 50_100_000)
    assert h.lc.phase is Phase.COOLDOWN and h.pos.is_flat


def test_lost_ack_does_not_cause_duplicate_order(make):
    h = make({0: E, 3_000: E}, paper={"fault_plan": {CID1: "drop_ack"}})
    h.trade(0, 50_000_000)
    for ts in range(1_000, 10_001, 1_000):
        h.trade(ts, 50_100_000)                  # 체결 안 되는 가격
    assert len(h.placed) == 1                    # 타임아웃에도 재주문하지 않음
    assert len(h.paper.orders) == 1
    codes = [a.code for a in h.engine.alerts]
    assert "ACK_TIMEOUT" in codes
    hist = [x[2] for x in h.engine.om.get(CID1).history]
    assert hist == ["UNKNOWN", "OPEN", "PENDING_CANCEL", "CANCELED"]  # 조회로 복구 → 타임아웃 취소
    assert h.lc.phase is Phase.FLAT


def test_lost_request_reconciles_to_rejected_then_new_id(make):
    h = make({0: E, 5_000: E}, paper={"fault_plan": {CID1: "lost"}})
    h.trade(0, 50_000_000)
    h.trade(2_000, 50_100_000)                   # ack 타임아웃 → 조회 → NOT_FOUND
    assert h.engine.om.get(CID1).status is OrderStatus.REJECTED
    assert h.lc.phase is Phase.FLAT
    h.trade(5_000, 50_000_000)
    assert h.placed[-1].client_id.endswith("ENTRY-000002")  # 새 ID로만 재주문


def test_reject_backoff_prevents_order_spam(make):
    h = make({0: E, 1_000: E, 2_000: E, 6_000: E}, paper={"fault_plan": {CID1: "reject"}})
    for ts in (0, 1_000, 2_000):
        h.trade(ts, 50_000_000)
    assert len(h.placed) == 1                    # 5초 백오프 동안 재시도 안 함
    h.trade(6_000, 50_000_000)
    assert len(h.placed) == 2


def test_reentry_blocked_during_cooldown_after_stop(make):
    h = make({0: E, 4_000: E, 60_000: E, 1_804_000: E})
    h.trade(0, 50_000_000)
    h.trade(1_000, 50_000_000)                   # 전량 체결
    assert h.lc.phase is Phase.OPEN and h.pos.qty == FULL
    h.trade(2_000, 49_000_000)                   # 손절선(평단×0.98=49,049,000) 이탈
    h.trade(3_000, 49_000_000)                   # 청산 체결
    assert h.lc.phase is Phase.COOLDOWN
    assert h.engine.closed_trades[-1]["reason"] == "STOP"
    h.trade(4_000, 49_000_000)
    h.trade(60_000, 49_000_000)
    assert len(h.placed) == 2                    # 쿨다운 중 진입 신호 무시
    h.trade(1_803_000, 49_000_000)               # 손절 쿨다운 30분 경과
    assert h.lc.phase is Phase.FLAT
    h.trade(1_804_000, 49_000_000)
    assert len(h.placed) == 3 and h.lc.phase is Phase.ENTERING


def test_partial_exit_retries_remaining_qty(make):
    h = make({0: E, 2_000: X})
    h.trade(0, 50_000_000)
    h.trade(1_000, 50_000_000)                   # 진입 전량 체결
    h.trade(2_000, 50_000_000)                   # 청산 신호 → 매도 49,950,000
    h.trade(3_000, 50_000_000, qty="0.001")      # 0.001만 매도됨
    for ts in range(4_000, 12_001, 1_000):
        h.trade(ts, 49_900_000)                  # 지정가 아래 → 체결 안 됨
    assert h.lc.phase is Phase.OPEN and h.lc.pending_exit_reason == "SIGNAL"
    h.trade(13_000, 49_900_000)
    assert h.placed[-1].client_id.endswith("EXIT-000003")  # 순번은 심볼 단위로 증가
    assert h.placed[-1].qty == FULL - D("0.001")


def _ev(cls, ts, **kw):
    _ev.seq += 1
    return cls(ts=ts, source="test", seq=_ev.seq, **kw)
_ev.seq = 0


def test_duplicate_fill_is_idempotent(make):
    h = make({0: E})
    h.paper.cfg.fault_plan[CID1] = "no_fill"
    h.trade(0, 50_000_000)
    fill = _ev(OrderFilled, 500, client_id=CID1, fill_id="F1", price=D(50_000_000), qty=D("0.001"), fee=D(25))
    h.engine.on_event(fill)
    cash = h.engine.cash
    h.engine.on_event(fill)                      # 같은 체결 재수신 (재연결 등)
    assert h.pos.qty == D("0.001") and h.engine.cash == cash
    assert "DUP_FILL" in [a.code for a in h.engine.alerts]


def test_late_fill_after_cancel_is_booked_and_reconciled(make):
    h = make({0: E})
    h.paper.cfg.fault_plan[CID1] = "no_fill"
    h.trade(0, 50_000_000)
    h.engine.on_event(_ev(OrderCanceled, 500, client_id=CID1))
    assert h.lc.phase is Phase.FLAT
    h.engine.on_event(_ev(OrderFilled, 600, client_id=CID1, fill_id="F9",
                          price=D(50_000_000), qty=D("0.001"), fee=D(25)))
    assert h.pos.qty == D("0.001")               # 돈은 이미 움직였다 → 장부 반영
    assert h.lc.phase is Phase.OPEN               # 라이프사이클도 실제 보유와 일치하게
    assert any(a.code == "LATE_FILL" and a.level == "CRITICAL" for a in h.engine.alerts)


def test_max_drawdown_halts_and_flattens(make):
    h = make({0: E, 10_000: E}, risk=RiskConfig(max_drawdown=D("0.005"), stop_pct=D("0.5"), risk_per_trade=D("0.5")))
    h.trade(0, 50_000_000)
    h.trade(1_000, 50_000_000)
    h.trade(2_000, 48_000_000)                   # 평가손 → 낙폭 0.5% 초과
    assert h.engine.halted
    h.trade(3_000, 48_000_000)
    assert h.pos.is_flat and h.engine.closed_trades[-1]["reason"] == "HALT"
    h.trade(10_000, 48_000_000)
    assert h.lc.phase is Phase.COOLDOWN and len(h.placed) == 2  # 신규 진입 없음


def test_order_manager_refuses_second_active_order():
    om = OrderManager()
    first = om.create(SYM, Side.BUY, D(1), D(1), "ENTRY", 0)
    with pytest.raises(DuplicateOrderError):
        om.create(SYM, Side.BUY, D(1), D(1), "ENTRY", 0)
    first.transition(OrderStatus.UNKNOWN, 1, "ack timeout")
    with pytest.raises(DuplicateOrderError):          # 상태 불명도 '활성'으로 취급
        om.create(SYM, Side.BUY, D(1), D(1), "ENTRY", 2)
    first.transition(OrderStatus.REJECTED, 3, "not found")
    om.release(first)
    assert om.create(SYM, Side.BUY, D(1), D(1), "ENTRY", 4).client_id.endswith("000002")
