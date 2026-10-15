"""[필수 검증] 가격 기준점의 명시적 정의 + 주문 상태 머신/거래소/파서 단위 테스트"""
from __future__ import annotations

from decimal import Decimal

import pytest

from autotrader.core.engine import Engine, EngineConfig
from autotrader.domain.events import (CancelOrder, MarketTrade, OrderAccepted, OrderFilled,
                                      PlaceOrder, QueryOrder, event_from_json, event_to_json)
from autotrader.domain.lifecycle import IllegalPhaseTransition, Lifecycle, Phase
from autotrader.domain.order import InvalidTransition, Order, OrderStatus as S, OverfillError
from autotrader.domain.position import Position, PositionError, PriceRef
from autotrader.domain.types import D, Side
from autotrader.exchange.paper import PaperConfig, PaperExchange
from autotrader.exchange.upbit_feed import parse_trade
from autotrader.risk.manager import RiskConfig, RiskManager
from autotrader.strategy.breakout import Signal

from conftest import SYM, ScriptedStrategy


# ------------------------------------------------------------ price reference
def test_position_reference_prices():
    p = Position(SYM)
    p.on_fill(Side.BUY, D(100), D(1), D0 := D(0))
    p.on_fill(Side.BUY, D(90), D(3), D0)
    p.mark = D(95)
    assert p.ref_price(PriceRef.ENTRY) == D(100)
    assert p.ref_price(PriceRef.AVG) == D("92.5")
    assert p.ref_price(PriceRef.MARK) == D(95)
    pnl = p.on_fill(Side.SELL, D(95), D(2), D0)
    assert pnl == D(5)                                  # (95 - 92.5) × 2
    assert p.ref_price(PriceRef.AVG) == D("92.5")       # 일부 매도 후에도 평단 유지
    p.on_fill(Side.SELL, D(95), D(2), D0)
    assert p.is_flat and p.cost == 0 and p.entry_price is None
    with pytest.raises(PositionError):
        p.ref_price(PriceRef.AVG)                       # 포지션 없으면 기준가도 없음 (조용히 0 반환 금지)


@pytest.mark.parametrize("ref, expect_stop", [(PriceRef.ENTRY, True), (PriceRef.AVG, False)])
def test_stop_uses_configured_reference_consistently(ref, expect_stop):
    """분할 체결 50M, 49M → 진입가 50M / 평단 ~49.5M. 현재가 48.8M에서
    ENTRY 기준 손절선(49.0M)은 이탈, AVG 기준 손절선(~48.5M)은 미이탈"""
    eng = Engine(EngineConfig(symbols=(SYM,)), ScriptedStrategy({0: Signal.ENTER}),
                 RiskManager(RiskConfig(stop_ref=ref)))
    seq = iter(range(1000))
    mk = lambda cls, ts, **kw: cls(ts=ts, source="t", seq=next(seq), **kw)
    cmds = eng.on_event(mk(MarketTrade, 0, symbol=SYM, price=D(50_000_000), qty=D(1)))
    cid, qty = cmds[0].client_id, cmds[0].qty
    eng.on_event(mk(OrderAccepted, 1, client_id=cid))
    eng.on_event(mk(OrderFilled, 2, client_id=cid, fill_id="a", price=D(50_000_000), qty=D("0.003"), fee=D(0)))
    eng.on_event(mk(OrderFilled, 3, client_id=cid, fill_id="b", price=D(49_000_000), qty=qty - D("0.003"), fee=D(0)))
    assert eng.lifecycles[SYM].phase is Phase.OPEN
    cmds = eng.on_event(mk(MarketTrade, 4, symbol=SYM, price=D(48_800_000), qty=D(1)))
    sells = [c for c in cmds if isinstance(c, PlaceOrder) and c.side is Side.SELL]
    assert bool(sells) is expect_stop


def test_sizing_risks_exactly_target_fraction():
    rm = RiskManager(RiskConfig(max_position_pct=D(1), risk_per_trade=D("0.01"), stop_pct=D("0.02")))
    equity, price = D(10_000_000), D(50_000_000)
    qty = rm.size(equity, equity, price)
    loss_at_stop = qty * price * D("0.02")
    assert D(99_999) <= loss_at_stop <= D(100_000)    # 자본의 1% (수량 내림 때문에 약간 작음)


def test_sizing_respects_min_notional():
    rm = RiskManager()
    assert rm.size(D(10_000), D(10_000), D(50_000_000)) == 0  # 3,000원 < 최소 5,000원


def test_float_is_rejected():
    with pytest.raises(TypeError):
        D(0.1)


# --------------------------------------------------------- state machines
def test_order_state_machine():
    o = Order("c1", SYM, Side.BUY, D(100), D(10), "ENTRY", 0)
    o.transition(S.OPEN, 1, "ack")
    o.transition(S.OPEN, 2, "dup ack")                 # 멱등
    assert o.apply_fill("f1", D(100), D(4), D(0), 3)
    assert not o.apply_fill("f1", D(100), D(4), D(0), 4)  # 중복 체결 무시
    assert o.status is S.PARTIALLY_FILLED and o.filled_qty == 4
    with pytest.raises(OverfillError):
        o.apply_fill("f2", D(100), D(7), D(0), 5)
    o.apply_fill("f3", D(100), D(6), D(0), 6)
    assert o.status is S.FILLED
    with pytest.raises(InvalidTransition):
        o.transition(S.CANCELED, 7, "too late")


def test_fill_before_ack_is_allowed():
    o = Order("c1", SYM, Side.BUY, D(100), D(10), "ENTRY", 0)
    o.apply_fill("f1", D(100), D(10), D(0), 1)          # 체결 통보가 ack보다 먼저 도착
    assert o.status is S.FILLED


def test_lifecycle_rejects_illegal_transition():
    lc = Lifecycle(SYM)
    with pytest.raises(IllegalPhaseTransition):
        lc.to(Phase.EXITING, 0, "no position")
    lc.to(Phase.ENTERING, 0, "x")
    with pytest.raises(IllegalPhaseTransition):
        lc.to(Phase.COOLDOWN, 0, "skip")


# ----------------------------------------------------------- paper exchange
def test_paper_duplicate_client_id_and_partial_fills():
    px = PaperExchange(PaperConfig(participation=D("0.5")))
    po = PlaceOrder("c1", SYM, Side.BUY, D(100), D(3))
    assert type(px.handle(po)[0]).__name__ == "OrderAccepted"
    assert px.handle(po)[0].reason == "DUPLICATE_CLIENT_ID"
    fills = px.on_market(MarketTrade(ts=1, source="m", seq=0, symbol=SYM, price=D(99), qty=D(2)))
    assert [f.qty for f in fills] == [D(1)]            # 체결량 2 × 참여율 0.5
    rep = px.handle(QueryOrder("c1"))[0]
    assert rep.status == "PARTIALLY_FILLED" and len(rep.fills) == 1
    assert px.handle(QueryOrder("zzz"))[0].status == "NOT_FOUND"
    assert type(px.handle(CancelOrder("c1"))[0]).__name__ == "OrderCanceled"


# ------------------------------------------------------------- serialization
def test_event_json_roundtrip_keeps_decimal_exact():
    e = OrderFilled(ts=1, source="paper", seq=3, client_id="c", fill_id="f",
                    price=D("50050000"), qty=D("0.00599400"), fee=D("149.99985"))
    back = event_from_json(event_to_json(e))
    assert back == e and isinstance(back.qty, Decimal)


def test_upbit_trade_parse():
    import json
    raw = ('{"type":"trade","code":"KRW-BTC","trade_price":143250000.0,"trade_volume":0.00213,'
           '"ask_bid":"BID","trade_timestamp":1760400000123,"sequential_id":17604000001230000}')
    msg = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
    t = parse_trade(msg, "upbit", 0)
    assert t.price == D("143250000.0") and t.qty == D("0.00213") and t.ts == 1760400000123
