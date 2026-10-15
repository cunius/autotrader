"""테스트 헬퍼: 신호를 시각별로 지정하는 스크립트 전략 + 엔진/페이퍼 조립"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from autotrader.core.engine import Engine, EngineConfig
from autotrader.core.runner import Runner
from autotrader.domain.events import MarketTrade, PlaceOrder
from autotrader.domain.types import D
from autotrader.exchange.paper import PaperConfig, PaperExchange
from autotrader.risk.manager import RiskConfig, RiskManager
from autotrader.strategy.breakout import Signal

SYM = "KRW-BTC"


class ScriptedStrategy:
    def __init__(self, signals: dict[int, Signal] | None = None) -> None:
        self.signals = signals or {}

    def on_trade(self, symbol, ts, price):
        return self.signals.get(ts)

    def snapshot(self):
        return {}


@dataclass
class Harness:
    engine: Engine
    paper: PaperExchange
    runner: Runner
    placed: list[PlaceOrder] = field(default_factory=list)
    _seq: int = 0

    def trade(self, ts: int, price, qty="1") -> None:
        self.runner.process(MarketTrade(ts=ts, source="mkt", seq=self._seq, symbol=SYM,
                                        price=D(price), qty=D(qty)))
        self._seq += 1

    @property
    def pos(self):
        return self.engine.positions[SYM]

    @property
    def lc(self):
        return self.engine.lifecycles[SYM]


@pytest.fixture
def make():
    def _make(signals=None, risk: RiskConfig | None = None, paper: dict | None = None, **eng) -> Harness:
        engine = Engine(EngineConfig(symbols=(SYM,), **eng), ScriptedStrategy(signals), RiskManager(risk))
        px = PaperExchange(PaperConfig(**(paper or {})))
        h = Harness(engine, px, None)  # type: ignore[arg-type]

        def hook(e, cmds):
            h.placed.extend(c for c in cmds if isinstance(c, PlaceOrder))

        h.runner = Runner(engine, px, on_processed=hook)
        return h
    return _make
