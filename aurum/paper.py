"""Paper trading engine. There is no broker order path here by design."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .bars import Bar, parse_timestamp
from .config import RunConfig
from .costs import CostModel
from .engine import performance_metrics
from .models import EquityPoint, Position, Side, Signal, Trade
from .risk import RiskManager
from .strategies import make_strategy


class PaperEngine:
    mode = "PAPER_ONLY"

    def __init__(self, config: RunConfig, state_path: str | Path) -> None:
        self.config = config
        self.state_path = Path(state_path)
        self.strategy = make_strategy(config)
        self.costs = CostModel(config.costs)
        self.risk = RiskManager(config.risk)
        self.state = self._load()
        self.risk.load_dict(self.state.get("risk"))
        self.bar_index = int(self.state.get("bar_index", 0))

    # -- lifecycle ---------------------------------------------------------
    def initialize_history(self, bars: list[Bar]) -> None:
        if self.state.get("last_timestamp") is not None or not bars:
            return
        history = bars[: -1] if len(bars) > 1 else []
        if not history:
            return
        depth = self.strategy.history_depth
        self.state["bars"] = [self._bar_dict(bar) for bar in history[-depth:]]
        self.state["last_timestamp"] = history[-1].timestamp.isoformat()
        self.state["status"] = "history_ready"
        self._persist()

    def process_bar(self, bar: Bar) -> dict[str, Any]:
        if bar.symbol != self.config.symbol:
            raise ValueError(f"paper state tracks {self.config.symbol}, got {bar.symbol}")
        last = self.state.get("last_timestamp")
        if last is not None and bar.timestamp <= parse_timestamp(last):
            return self.state
        history = self._history()
        if history and history[-1].timestamp >= bar.timestamp:
            return self.state
        self.bar_index += 1
        history.append(bar)
        depth = self.strategy.history_depth
        self.state["bars"] = [self._bar_dict(item) for item in history[-depth:]]
        self.risk.load_dict(self.state.get("risk"))

        position = self._position()
        pending = self.state.get("pending")
        cash = float(self.state.get("cash", self.config.risk.capital))
        equity_open = self._equity(cash, position, bar.open)
        self.risk.start_day(bar.timestamp, equity_open)

        if pending is not None:
            if pending.get("kind") == "enter" and position is None:
                position = self._open(Signal.from_dict(pending["signal"]), bar, self.bar_index, equity_open)
            elif pending.get("kind") == "exit" and position is not None:
                cash, trade = self._close(position, bar.open, bar.timestamp, self.bar_index, pending.get("reason", "exit"), cash)
                self._append_trade(trade)
                position = None
            self.state["pending"] = None

        if position is not None:
            position, cash, closed = self._intrabar(position, bar, self.bar_index, cash)
            if closed is not None:
                self._append_trade(closed)
            if position is not None:
                position.bars_held += 1
                if position.max_holding_bars and position.bars_held >= position.max_holding_bars:
                    cash, trade = self._close(position, bar.close, bar.timestamp, self.bar_index, "max_holding", cash)
                    self._append_trade(trade)
                    position = None

        equity = self._equity(cash, position, bar.close)
        if position is not None and self.risk.daily_loss_breached(bar.timestamp, equity):
            cash, trade = self._close(position, bar.close, bar.timestamp, self.bar_index, "daily_loss_halt", cash)
            self._append_trade(trade)
            position = None
            equity = cash

        self.state["position"] = position.to_dict() if position else None
        self.state["cash"] = cash
        self.state["equity"] = equity
        self.state["last_timestamp"] = bar.timestamp.isoformat()
        self.state["last_price"] = bar.close
        self.state["bar_index"] = self.bar_index
        self.state["risk"] = self.risk.to_dict()
        self.state["status"] = "flat" if position is None else "position_open"
        self._append_equity(bar, equity, position)
        self.state["metrics"] = self._metrics()

        if position is None and self.state.get("pending") is None:
            signal = self.strategy.signal(history)
            if signal is not None and self.risk.can_enter(
                bar.timestamp, equity, bar.close, signal.stop_distance, self.bar_index
            ):
                self.state["pending"] = {"kind": "enter", "signal": signal.to_dict()}
        elif position is not None and self.state.get("pending") is None:
            if self.strategy.should_flatten(bar):
                self.state["pending"] = {"kind": "exit", "reason": "session_flatten"}
            elif self.strategy.exit_signal(position, history):
                self.state["pending"] = {"kind": "exit", "reason": "strategy_exit"}
            else:
                signal = self.strategy.signal(history)
                if signal is not None and signal.side is not position.side:
                    self.state["pending"] = {"kind": "exit", "reason": "opposing_signal"}
        self._persist()
        return self.state

    def reset(self) -> dict[str, Any]:
        self.state = self._new_state()
        self.risk = RiskManager(self.config.risk)
        self._persist()
        return self.state

    # -- internals ---------------------------------------------------------
    def _new_state(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "broker": "none (paper)",
            "symbol": self.config.symbol,
            "strategy": self.config.strategy,
            "capital": self.config.risk.capital,
            "cash": self.config.risk.capital,
            "equity": self.config.risk.capital,
            "position": None,
            "pending": None,
            "trades": [],
            "equity_curve": [],
            "bars": [],
            "risk": self.risk.to_dict(),
            "last_timestamp": None,
            "last_price": None,
            "status": "waiting_for_bar",
            "metrics": {},
        }

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._new_state()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("symbol") != self.config.symbol:
            raise ValueError("paper state symbol does not match configuration")
        return state

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.state, indent=2, allow_nan=False, default=str), encoding="utf-8")
        temporary.replace(self.state_path)

    def _history(self) -> list[Bar]:
        return [Bar(
            timestamp=parse_timestamp(item["timestamp"]),
            symbol=item["symbol"],
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
            volume=int(item.get("volume", 0)),
            expiry=item.get("expiry") or None,
        ) for item in self.state.get("bars", [])]

    def _position(self) -> Position | None:
        value = self.state.get("position")
        return Position.from_dict(value) if value else None

    def _equity(self, cash: float, position: Position | None, price: float) -> float:
        return cash if position is None else cash + position.unrealized(price) - position.entry_fee

    def _open(self, signal: Signal, bar: Bar, index: int, equity: float) -> Position | None:
        buying = signal.side is Side.LONG
        entry_price = self.costs.fill_price(bar.open, buying)
        decision = self.risk.size(equity, entry_price, signal.stop_distance, signal.confidence)
        if decision.quantity < 1:
            return None
        entry_fee = self.costs.charges(buying, entry_price, decision.quantity, self.config.risk.multiplier).total
        self.risk.register_entry(index)
        return Position(
            symbol=signal.symbol,
            side=signal.side,
            quantity=decision.quantity,
            entry_price=entry_price,
            entry_time=bar.timestamp,
            stop_price=entry_price - signal.side.sign * signal.stop_distance,
            target_price=entry_price + signal.side.sign * signal.stop_distance * signal.target_r,
            entry_fee=entry_fee,
            multiplier=self.config.risk.multiplier,
            margin=decision.margin,
            max_holding_bars=self.strategy.max_holding_bars,
            reason=signal.reason,
        )

    def _intrabar(self, position: Position, bar: Bar, index: int, cash: float) -> tuple[Position | None, float, Trade | None]:
        long = position.side is Side.LONG
        if long:
            if bar.open <= position.stop_price:
                cash, trade = self._close(position, bar.open, bar.timestamp, index, "gap_stop", cash)
                return None, cash, trade
            if bar.low <= position.stop_price:
                cash, trade = self._close(position, position.stop_price, bar.timestamp, index, "stop_loss", cash)
                return None, cash, trade
            if bar.open >= position.target_price:
                cash, trade = self._close(position, bar.open, bar.timestamp, index, "gap_target", cash)
                return None, cash, trade
            if bar.high >= position.target_price:
                cash, trade = self._close(position, position.target_price, bar.timestamp, index, "take_profit", cash)
                return None, cash, trade
        else:
            if bar.open >= position.stop_price:
                cash, trade = self._close(position, bar.open, bar.timestamp, index, "gap_stop", cash)
                return None, cash, trade
            if bar.high >= position.stop_price:
                cash, trade = self._close(position, position.stop_price, bar.timestamp, index, "stop_loss", cash)
                return None, cash, trade
            if bar.open <= position.target_price:
                cash, trade = self._close(position, bar.open, bar.timestamp, index, "gap_target", cash)
                return None, cash, trade
            if bar.low <= position.target_price:
                cash, trade = self._close(position, position.target_price, bar.timestamp, index, "take_profit", cash)
                return None, cash, trade
        return position, cash, None

    def _close(self, position: Position, reference: float, timestamp: datetime, index: int, reason: str, cash: float) -> tuple[float, Trade]:
        closing_buy = position.side is Side.SHORT
        exit_price = self.costs.fill_price(reference, closing_buy)
        exit_fee = self.costs.charges(closing_buy, exit_price, position.quantity, position.multiplier).total
        gross = (exit_price - position.entry_price) * position.side.sign * position.quantity * position.multiplier
        net = gross - position.entry_fee - exit_fee
        cash += gross - position.entry_fee - exit_fee
        self.risk.register_exit(net, index)
        trade = Trade(
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            entry_time=position.entry_time,
            exit_time=timestamp,
            entry_price=position.entry_price,
            exit_price=exit_price,
            gross_pnl=gross,
            fees=position.entry_fee + exit_fee,
            net_pnl=net,
            entry_reason=position.reason,
            exit_reason=reason,
            bars_held=position.bars_held,
            multiplier=position.multiplier,
        )
        return cash, trade

    def _append_trade(self, trade: Trade) -> None:
        self.state.setdefault("trades", []).append(trade.to_dict())

    def _append_equity(self, bar: Bar, equity: float, position: Position | None) -> None:
        curve = self.state.setdefault("equity_curve", [])
        previous_peak = max([float(self.config.risk.capital)] + [float(item["equity"]) for item in curve])
        peak = max(previous_peak, equity)
        curve.append(
            EquityPoint(
                timestamp=bar.timestamp,
                equity=equity,
                drawdown=equity / peak - 1.0 if peak else 0.0,
                price=bar.close,
                position=position.quantity if position else 0,
            ).to_dict()
        )
        if len(curve) > 6000:
            del curve[:-6000]

    def _metrics(self) -> dict[str, Any]:
        curve = [EquityPoint.from_dict(item) for item in self.state.get("equity_curve", [])]
        trades = [Trade.from_dict(item) for item in self.state.get("trades", [])]
        return performance_metrics(curve, trades, self.config.risk.capital, self.config.bar_minutes)

    @staticmethod
    def _bar_dict(bar: Bar) -> dict[str, Any]:
        return {
            "timestamp": bar.timestamp.isoformat(),
            "symbol": bar.symbol,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "expiry": bar.expiry,
        }


__all__ = ["PaperEngine"]
