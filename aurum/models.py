"""Domain models shared by engine, strategies and the paper trader."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


def parse_side(value: str | Side) -> Side:
    return value if isinstance(value, Side) else Side(str(value))


@dataclass
class Signal:
    timestamp: datetime
    symbol: str
    side: Side
    confidence: float
    reason: str
    atr: float
    stop_distance: float
    target_r: float = 2.0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "side": self.side.value,
            "confidence": round(self.confidence, 6),
            "reason": self.reason,
            "atr": round(self.atr, 6),
            "stop_distance": round(self.stop_distance, 6),
            "target_r": round(self.target_r, 6),
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Signal":
        return cls(
            timestamp=datetime.fromisoformat(value["timestamp"]),
            symbol=value["symbol"],
            side=parse_side(value["side"]),
            confidence=float(value["confidence"]),
            reason=value["reason"],
            atr=float(value["atr"]),
            stop_distance=float(value["stop_distance"]),
            target_r=float(value.get("target_r", 2.0)),
            meta=value.get("meta", {}),
        )


@dataclass
class Position:
    symbol: str
    side: Side
    quantity: int
    entry_price: float
    entry_time: datetime
    stop_price: float
    target_price: float
    entry_fee: float
    multiplier: float
    margin: float
    max_holding_bars: int
    bars_held: int = 0
    reason: str = ""

    def unrealized(self, price: float) -> float:
        return (price - self.entry_price) * self.side.sign * self.quantity * self.multiplier

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat(),
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "entry_fee": self.entry_fee,
            "multiplier": self.multiplier,
            "margin": self.margin,
            "max_holding_bars": self.max_holding_bars,
            "bars_held": self.bars_held,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Position":
        return cls(
            symbol=value["symbol"],
            side=parse_side(value["side"]),
            quantity=int(value["quantity"]),
            entry_price=float(value["entry_price"]),
            entry_time=datetime.fromisoformat(value["entry_time"]),
            stop_price=float(value["stop_price"]),
            target_price=float(value["target_price"]),
            entry_fee=float(value["entry_fee"]),
            multiplier=float(value["multiplier"]),
            margin=float(value["margin"]),
            max_holding_bars=int(value["max_holding_bars"]),
            bars_held=int(value.get("bars_held", 0)),
            reason=value.get("reason", ""),
        )


@dataclass
class Trade:
    symbol: str
    side: Side
    quantity: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    gross_pnl: float
    fees: float
    net_pnl: float
    entry_reason: str
    exit_reason: str
    bars_held: int
    multiplier: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "gross_pnl": self.gross_pnl,
            "fees": self.fees,
            "net_pnl": self.net_pnl,
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
            "bars_held": self.bars_held,
            "multiplier": self.multiplier,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Trade":
        return cls(
            symbol=value["symbol"],
            side=parse_side(value["side"]),
            quantity=int(value["quantity"]),
            entry_time=datetime.fromisoformat(value["entry_time"]),
            exit_time=datetime.fromisoformat(value["exit_time"]),
            entry_price=float(value["entry_price"]),
            exit_price=float(value["exit_price"]),
            gross_pnl=float(value["gross_pnl"]),
            fees=float(value["fees"]),
            net_pnl=float(value["net_pnl"]),
            entry_reason=value.get("entry_reason", ""),
            exit_reason=value.get("exit_reason", ""),
            bars_held=int(value.get("bars_held", 0)),
            multiplier=float(value.get("multiplier", 1.0)),
        )


@dataclass
class EquityPoint:
    timestamp: datetime
    equity: float
    drawdown: float
    price: float
    position: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "equity": self.equity,
            "drawdown": self.drawdown,
            "price": self.price,
            "position": self.position,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EquityPoint":
        return cls(
            timestamp=datetime.fromisoformat(value["timestamp"]),
            equity=float(value["equity"]),
            drawdown=float(value["drawdown"]),
            price=float(value["price"]),
            position=int(value["position"]),
        )


@dataclass
class BacktestResult:
    symbol: str
    initial_capital: float
    final_equity: float
    metrics: dict[str, Any]
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    signals: list[Signal]
    config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "initial_capital": self.initial_capital,
            "final_equity": self.final_equity,
            "metrics": self.metrics,
            "trades": [trade.to_dict() for trade in self.trades],
            "equity_curve": [point.to_dict() for point in self.equity_curve],
            "signals": [signal.to_dict() for signal in self.signals],
            "config": self.config,
            "disclaimer": "Research and paper trading only. Past results do not imply future profits.",
        }
