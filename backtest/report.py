"""백테스트 결과 요약"""
from __future__ import annotations

from decimal import Decimal

from ..core.engine import Engine


def summarize(engine: Engine, equity_curve: list[Decimal]) -> dict:
    init = engine.cfg.initial_cash
    final = engine.equity()
    peak, mdd = init, Decimal(0)
    for v in equity_curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    pnls = [Decimal(t["pnl"]) for t in engine.closed_trades]
    wins = [p for p in pnls if p > 0]
    by_reason: dict[str, int] = {}
    for t in engine.closed_trades:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
    return {
        "initial_equity": f"{init:,.0f}",
        "final_equity": f"{final:,.0f}",
        "return_pct": f"{(final / init - 1) * 100:.2f}",
        "max_drawdown_pct": f"{mdd * 100:.2f}",
        "trades": len(pnls),
        "win_rate_pct": f"{(len(wins) / len(pnls) * 100) if pnls else 0:.1f}",
        "exit_reasons": by_reason,
        "alerts": len(engine.alerts),
        "halted": engine.halt_reason,
        "state_hash": engine.state_hash()[:16],
    }
