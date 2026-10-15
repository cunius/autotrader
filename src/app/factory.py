"""조립(Composition root). 설정 → 엔진/거래소 생성"""
from __future__ import annotations

from ..core.engine import Engine, EngineConfig
from ..domain.types import D
from ..exchange.paper import PaperConfig, PaperExchange
from ..risk.manager import RiskConfig, RiskManager
from ..strategy.breakout import BreakoutStrategy


def build_engine(symbols: list[str], initial_cash: str = "1000000",
                 bar_ms: int = 60_000, entry_lookback: int = 20, exit_lookback: int = 10,
                 risk: RiskConfig | None = None, **engine_kw) -> Engine:
    cfg = EngineConfig(symbols=tuple(symbols), initial_cash=D(initial_cash), **engine_kw)
    return Engine(cfg, BreakoutStrategy(bar_ms, entry_lookback, exit_lookback), RiskManager(risk))


def build_paper(**kw) -> PaperExchange:
    return PaperExchange(PaperConfig(**kw))
