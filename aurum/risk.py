"""Risk manager: volatility-based sizing and account guards."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .config import RiskConfig


@dataclass
class SizeDecision:
    quantity: int
    risk_budget: float
    notional: float
    margin: float


class RiskManager:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        self.day: date | None = None
        self.day_start_equity = self.config.capital
        self.trades_today = 0
        self.consecutive_losses = 0
        self.halted_today = False
        self.last_entry_bar: int | None = None
        self.last_exit_bar: int | None = None

    # -- day bookkeeping -------------------------------------------------
    def start_day(self, timestamp: datetime, equity: float) -> None:
        day = timestamp.date()
        if self.day != day:
            self.day = day
            self.day_start_equity = equity
            self.trades_today = 0
            self.consecutive_losses = 0
            self.halted_today = False

    # -- guards -----------------------------------------------------------
    def can_enter(self, timestamp: datetime, equity: float, price: float, stop_distance: float, bar_index: int) -> bool:
        self.start_day(timestamp, equity)
        if self.halted_today or self.trades_today >= self.config.max_trades_per_day:
            return False
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            return False
        if self.last_exit_bar is not None and bar_index - self.last_exit_bar <= self.config.cooldown_bars:
            return False
        return self.size(equity, price, stop_distance).quantity > 0

    def size(self, equity: float, price: float, stop_distance: float, confidence: float = 1.0) -> SizeDecision:
        if price <= 0 or stop_distance <= 0 or equity <= 0:
            return SizeDecision(0, 0.0, 0.0, 0.0)
        confidence = min(1.0, max(0.5, confidence))
        multiplier = max(self.config.multiplier, 1e-9)
        if self.config.sizing_mode == "notional":
            # notional targeting is the position's whole purpose; confidence
            # only tunes risk-budget sizing, it must not shrink the target here
            risk_budget = equity * self.config.risk_per_trade_pct * confidence
            by_risk = math.floor(equity * self.config.target_notional_pct / (price * multiplier))
        else:
            risk_budget = equity * self.config.risk_per_trade_pct * confidence
            by_risk = math.floor(risk_budget / (stop_distance * multiplier))
        by_notional = math.floor(equity * self.config.max_notional_pct / (price * multiplier))
        by_margin = math.floor(equity * self.config.max_margin_pct / (price * multiplier * self.config.margin_pct))
        quantity = max(0, min(by_risk, by_notional, by_margin, self.config.max_quantity))
        return SizeDecision(
            quantity=quantity,
            risk_budget=risk_budget,
            notional=quantity * price * multiplier,
            margin=quantity * price * multiplier * self.config.margin_pct,
        )

    def register_entry(self, bar_index: int) -> None:
        self.trades_today += 1
        self.last_entry_bar = bar_index

    def register_exit(self, pnl: float, bar_index: int) -> None:
        self.last_exit_bar = bar_index
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def daily_loss_breached(self, timestamp: datetime, equity: float) -> bool:
        self.start_day(timestamp, equity)
        if self.day_start_equity <= 0:
            return False
        loss = (self.day_start_equity - equity) / self.day_start_equity
        if loss >= self.config.daily_loss_pct:
            self.halted_today = True
        return self.halted_today

    # -- persistence ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat() if self.day else None,
            "day_start_equity": self.day_start_equity,
            "trades_today": self.trades_today,
            "consecutive_losses": self.consecutive_losses,
            "halted_today": self.halted_today,
            "last_entry_bar": self.last_entry_bar,
            "last_exit_bar": self.last_exit_bar,
        }

    def load_dict(self, value: dict[str, Any] | None) -> None:
        value = value or {}
        day = value.get("day")
        self.day = date.fromisoformat(day) if day else None
        self.day_start_equity = float(value.get("day_start_equity", self.config.capital))
        self.trades_today = int(value.get("trades_today", 0))
        self.consecutive_losses = int(value.get("consecutive_losses", 0))
        self.halted_today = bool(value.get("halted_today", False))
        self.last_entry_bar = value.get("last_entry_bar")
        self.last_exit_bar = value.get("last_exit_bar")
