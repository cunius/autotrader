"""저널 리플레이: 라이브 실행을 그대로 재현

    python -m autotrader.app.replay journal/20261014-210000.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..core.journal import replay
from .factory import build_engine


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("journal")
    args = ap.parse_args()
    cfg = json.loads(Path(args.journal.replace(".jsonl", ".config.json")).read_text())
    engine = replay(args.journal, lambda: build_engine(
        cfg["symbols"], initial_cash=cfg["cash"], bar_ms=cfg["bar_sec"] * 1000,
        entry_lookback=cfg["entry_lookback"], exit_lookback=cfg["exit_lookback"]))
    print(json.dumps({"events": engine.events_processed, "equity": str(engine.equity()),
                      "state_hash": engine.state_hash()[:16],
                      "phases": {s: l.phase.value for s, l in engine.lifecycles.items()},
                      "alerts": [a.__dict__ for a in engine.alerts[-10:]]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
