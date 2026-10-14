"""결정적 트레이딩 엔진 (단일 스레드 상태 기계)

    on_event(event) -> list[Command]

- 입력은 이벤트뿐, 출력은 커맨드뿐. I/O·벽시계·난수를 쓰지 않는다.
- '현재 시각'은 처리 중인 이벤트의 ts에서만 얻는다 (self.now).
- 따라서 같은 이벤트 열을 넣으면 상태 해시(state_hash)가 항상 같다.
  → 저널 리플레이로 장애 상황을 그대로 재현할 수 있다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

from ..domain.events import (Clock, Command, Event, MarketTrade, OrderAccepted, OrderCanceled,
                             OrderFilled, OrderRejected, OrderStatusReport, PlaceOrder,
                             CancelOrder, QueryOrder)
from ..domain.lifecycle import Lifecycle, Phase
from ..domain.order import InvalidTransition, Order, OrderStatus as S
from ..domain.position import Position
from ..domain.types import D, D0, Side, ceil_to, floor_to
from ..execution.order_manager import OrderManager
from ..risk.manager import DrawdownGuard, RiskManager
from ..strategy.breakout import BreakoutStrategy, Signal

MIN = 60_000


def _default_cooldowns() -> dict[str, int]:
    return {"STOP": 30 * MIN, "TAKE_PROFIT": 5 * MIN, "SIGNAL": 5 * MIN, "HALT": 10**15}


@dataclass(frozen=True)
class EngineConfig:
    symbols: tuple[str, ...]
    initial_cash: Decimal = D("1000000")
    price_tick: Decimal = D("1")          # 단순화: 실제 업비트는 가격대별 호가단위가 다름
    slippage_bps: Decimal = D("10")       # 시장가성 지정가 오프셋 (슬리피지 상한)
    ack_timeout_ms: int = 2_000           # 이 시간 내 ack 없으면 UNKNOWN → 조회
    order_timeout_ms: int = 10_000        # 미체결 잔량은 이 시간 후 취소
    reject_backoff_ms: int = 5_000
    cooldown_ms: dict[str, int] = field(default_factory=_default_cooldowns)


@dataclass(frozen=True)
class Alert:
    ts: int
    level: str   # INFO | WARN | CRITICAL
    code: str
    msg: str


class Engine:
    def __init__(self, cfg: EngineConfig, strategy: BreakoutStrategy, risk: RiskManager) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.risk = risk
        self.cash = cfg.initial_cash
        self.positions = {s: Position(s) for s in cfg.symbols}
        self.lifecycles = {s: Lifecycle(s) for s in cfg.symbols}
        self.om = OrderManager()
        self.dd = DrawdownGuard(risk.cfg.max_drawdown, cfg.initial_cash)
        self.halt_reason: str | None = None
        self.now = 0
        self.events_processed = 0
        self.alerts: list[Alert] = []
        self.closed_trades: list[dict] = []

    # ================================================================ API
    def on_event(self, e: Event) -> list[Command]:
        self.now = max(self.now, e.ts)
        self.events_processed += 1
        cmds: list[Command] = []
        if isinstance(e, MarketTrade):
            cmds += self._on_trade(e)
        elif isinstance(e, OrderAccepted):
            self._with_order(e.client_id, self._on_accepted)
        elif isinstance(e, OrderFilled):
            self._with_order(e.client_id, lambda o: self._apply_fill(o, e.fill_id, e.price, e.qty, e.fee))
        elif isinstance(e, OrderCanceled):
            self._with_order(e.client_id, lambda o: self._terminal(o, S.CANCELED, "exchange canceled"))
        elif isinstance(e, OrderRejected):
            self._with_order(e.client_id, lambda o: self._on_rejected(o, e.reason))
        elif isinstance(e, OrderStatusReport):
            self._with_order(e.client_id, lambda o: self._reconcile(o, e))
        elif isinstance(e, Clock):
            pass
        cmds += self._housekeeping()
        return cmds

    @property
    def halted(self) -> bool:
        return self.halt_reason is not None

    def equity(self) -> Decimal:
        total = self.cash
        for p in self.positions.values():
            if not p.is_flat:
                total += p.qty * (p.mark if p.mark is not None else p.avg_price)
        return total

    def snapshot(self) -> dict:
        return {
            "now": self.now,
            "cash": str(self.cash),
            "positions": {s: p.snapshot() for s, p in sorted(self.positions.items())},
            "lifecycles": {s: l.snapshot() for s, l in sorted(self.lifecycles.items())},
            "orders": self.om.snapshot(),
            "drawdown": self.dd.snapshot(),
            "halt_reason": self.halt_reason,
            "strategy": self.strategy.snapshot(),
            "alerts": [(a.level, a.code) for a in self.alerts],
            "closed_trades": self.closed_trades,
        }

    def state_hash(self) -> str:
        blob = json.dumps(self.snapshot(), sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()

    # ======================================================= market data
    def _on_trade(self, t: MarketTrade) -> list[Command]:
        pos = self.positions.get(t.symbol)
        if pos is None:
            return []
        pos.mark = t.price
        if self.dd.update(self.equity()):
            self._halt(f"max drawdown {self.dd.drawdown:.4f} ≥ {self.dd.max_drawdown}")

        signal = self.strategy.on_trade(t.symbol, t.ts, t.price)
        lc = self.lifecycles[t.symbol]
        if not lc.can_place_order(self.now):
            return []
        if lc.phase is Phase.OPEN:
            reason = (lc.pending_exit_reason or self.risk.exit_reason(pos)
                      or ("SIGNAL" if signal is Signal.EXIT else None))
            if reason:
                return self._place_exit(t.symbol, reason, t.price)
        elif lc.phase is Phase.FLAT and signal is Signal.ENTER and not self.halted:
            return self._place_entry(t.symbol, t.price)
        return []

    def _place_entry(self, symbol: str, price: Decimal) -> list[Command]:
        limit = ceil_to(price * (1 + self.cfg.slippage_bps / 10_000), self.cfg.price_tick)
        qty = self.risk.size(self.equity(), self.cash, limit)
        if qty <= 0:
            return []
        o = self.om.create(symbol, Side.BUY, limit, qty, "ENTRY", self.now)
        lc = self.lifecycles[symbol]
        lc.active_order = o.client_id
        lc.entry_equity_mark = self.positions[symbol].realized_pnl - self.positions[symbol].fees
        lc.to(Phase.ENTERING, self.now, "signal ENTER")
        return [PlaceOrder(o.client_id, symbol, Side.BUY, limit, qty)]

    def _place_exit(self, symbol: str, reason: str, price: Decimal) -> list[Command]:
        pos = self.positions[symbol]
        limit = floor_to(price * (1 - self.cfg.slippage_bps / 10_000), self.cfg.price_tick)
        o = self.om.create(symbol, Side.SELL, limit, pos.qty, "EXIT", self.now)
        lc = self.lifecycles[symbol]
        lc.active_order = o.client_id
        lc.pending_exit_reason = reason
        lc.to(Phase.EXITING, self.now, reason)
        return [PlaceOrder(o.client_id, symbol, Side.SELL, limit, pos.qty)]

    # ====================================================== order events
    def _with_order(self, cid: str, fn) -> None:
        o = self.om.get(cid)
        if o is None:
            self._alert("WARN", "UNKNOWN_ORDER", f"모르는 주문 이벤트: {cid}")
            return
        try:
            fn(o)
        except InvalidTransition as ex:
            # 상태 머신이 예상 못 한 상황 = 로직/거래소 불일치. 조용히 넘기지 않고 신규 진입 중단
            self._alert("CRITICAL", "INVALID_TRANSITION", str(ex))
            self._halt(f"invalid transition: {ex}")

    def _on_accepted(self, o: Order) -> None:
        if o.status in (S.PENDING_NEW, S.UNKNOWN):
            o.transition(S.OPEN, self.now, "ack")

    def _on_rejected(self, o: Order, reason: str) -> None:
        if o.is_terminal:
            self._alert("WARN", "REJECT_AFTER_TERMINAL", f"{o.client_id}: {reason}")
            return
        self.lifecycles[o.symbol].retry_after = self.now + self.cfg.reject_backoff_ms
        self._terminal(o, S.REJECTED, reason)

    def _apply_fill(self, o: Order, fill_id: str, price: Decimal, qty: Decimal, fee: Decimal) -> None:
        was_terminal = o.is_terminal
        if not o.apply_fill(fill_id, price, qty, fee, self.now):
            self._alert("INFO", "DUP_FILL", f"{o.client_id}/{fill_id} 중복 체결 무시")
            return
        pos = self.positions[o.symbol]
        pos.on_fill(o.side, price, qty, fee)
        if o.side is Side.BUY:
            self.cash -= price * qty + fee
        else:
            self.cash += price * qty - fee
        if was_terminal:
            self._alert("CRITICAL", "LATE_FILL", f"{o.client_id}: 종결 후 체결 {qty}@{price}")
            self._resync_phase(o.symbol, "late fill")
        elif o.is_terminal:
            self._on_order_done(o)

    def _terminal(self, o: Order, status: S, reason: str) -> None:
        if o.is_terminal:
            return
        o.transition(status, self.now, reason)
        self._on_order_done(o)

    def _reconcile(self, o: Order, r: OrderStatusReport) -> None:
        """조회 결과로 상태 맞추기. 체결 내역은 fill_id로 멱등 반영."""
        for f in r.fills:
            self._apply_fill(o, f.fill_id, f.price, f.qty, f.fee)
        if o.is_terminal:
            return
        if r.status == "NOT_FOUND":
            # 거래소에 주문이 없다 = 접수 자체가 안 됨 → 안전하게 '거부'로 종결 (새 ID로 재주문 가능)
            self._terminal(o, S.REJECTED, "not found at exchange")
        elif r.status == "CANCELED":
            self._terminal(o, S.CANCELED, "reconciled")
        elif r.status == "REJECTED":
            self._terminal(o, S.REJECTED, "reconciled")
        elif r.status == "FILLED":
            self._alert("CRITICAL", "RECONCILE_MISMATCH", f"{o.client_id}: 거래소 FILLED, 로컬 {o.filled_qty}/{o.qty}")
        elif o.status is S.UNKNOWN:
            o.transition(S.PARTIALLY_FILLED if o.filled_qty else S.OPEN, self.now, "reconciled")

    def _on_order_done(self, o: Order) -> None:
        """주문 종결 → 라이프사이클 전이. 여러 경로에서 호출돼도 한 번만 동작(멱등)."""
        self.om.release(o)
        lc = self.lifecycles[o.symbol]
        if lc.active_order != o.client_id:
            return
        lc.active_order = None
        pos = self.positions[o.symbol]
        if o.intent == "ENTRY":
            lc.to(Phase.OPEN if not pos.is_flat else Phase.FLAT, self.now,
                  f"entry {o.status.value} filled={o.filled_qty}")
        elif pos.is_flat:
            reason = lc.pending_exit_reason or "SIGNAL"
            self.closed_trades.append({
                "symbol": o.symbol, "exit_ts": self.now, "reason": reason,
                "pnl": str(pos.realized_pnl - pos.fees - (lc.entry_equity_mark or D0)),
            })
            lc.pending_exit_reason = None
            lc.cooldown_until = self.now + self.cfg.cooldown_ms.get(reason, 5 * MIN)
            lc.to(Phase.COOLDOWN, self.now, f"exit done ({reason})")
        else:
            lc.to(Phase.OPEN, self.now, f"exit {o.status.value}, 잔량 {pos.qty} → 재시도")

    def _resync_phase(self, symbol: str, why: str) -> None:
        lc = self.lifecycles[symbol]
        if lc.active_order is not None:
            return  # 활성 주문이 끝날 때 정상 경로로 정리됨
        has_pos = not self.positions[symbol].is_flat
        if has_pos and lc.phase in (Phase.FLAT, Phase.COOLDOWN):
            lc.force(Phase.OPEN, self.now, why)
        elif not has_pos and lc.phase is Phase.OPEN:
            lc.force(Phase.FLAT, self.now, why)

    # ====================================================== housekeeping
    def _housekeeping(self) -> list[Command]:
        cmds: list[Command] = []
        for o in self.om.active_orders():
            age = self.now - o.created_ts
            since = self.now - o.status_ts
            if o.status is S.PENDING_NEW and age >= self.cfg.ack_timeout_ms:
                # 타임아웃 ≠ 실패. 재주문하지 말고 조회한다.
                o.transition(S.UNKNOWN, self.now, "ack timeout")
                self._alert("WARN", "ACK_TIMEOUT", o.client_id)
                cmds.append(QueryOrder(o.client_id))
            elif o.status in (S.OPEN, S.PARTIALLY_FILLED) and age >= self.cfg.order_timeout_ms:
                o.transition(S.PENDING_CANCEL, self.now, "order timeout")
                cmds.append(CancelOrder(o.client_id))
            elif o.status in (S.UNKNOWN, S.PENDING_CANCEL) and since >= self.cfg.ack_timeout_ms:
                if o.status is S.PENDING_CANCEL:
                    o.transition(S.UNKNOWN, self.now, "cancel ack timeout")
                o.status_ts = self.now  # 재조회 주기
                cmds.append(QueryOrder(o.client_id))
        for s in sorted(self.lifecycles):
            lc = self.lifecycles[s]
            if lc.phase is Phase.COOLDOWN and self.now >= lc.cooldown_until and not self.halted:
                lc.to(Phase.FLAT, self.now, "cooldown elapsed")
        return cmds

    # ============================================================ misc
    def _halt(self, reason: str) -> None:
        if self.halt_reason is None:
            self.halt_reason = reason
            self._alert("CRITICAL", "HALT", reason)
        for s in sorted(self.lifecycles):
            lc = self.lifecycles[s]
            if not self.positions[s].is_flat:
                lc.pending_exit_reason = "HALT"

    def _alert(self, level: str, code: str, msg: str) -> None:
        self.alerts.append(Alert(self.now, level, code, msg))
