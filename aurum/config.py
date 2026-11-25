"""Configuration objects for AURUM runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CostConfig:
    slippage_bps: float = 1.5
    brokerage_per_order: float = 20.0
    brokerage_pct: float = 0.0003
    exchange_pct: float = 0.000019
    stt_sell_pct: float = 0.0002
    stamp_buy_pct: float = 0.00002
    sebi_pct: float = 0.000001
    gst_pct: float = 0.18

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "CostConfig":
        value = value or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: float(v) for k, v in value.items() if k in known})


@dataclass
class RiskConfig:
    capital: float = 1_000_000.0
    risk_per_trade_pct: float = 0.01
    sizing_mode: str = "risk"
    target_notional_pct: float = 1.0
    max_notional_pct: float = 1.5
    margin_pct: float = 0.15
    max_margin_pct: float = 0.5
    max_quantity: int = 25
    multiplier: float = 1.0
    daily_loss_pct: float = 0.04
    max_trades_per_day: int = 2
    max_consecutive_losses: int = 6
    cooldown_bars: int = 1

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "RiskConfig":
        value = value or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in value.items() if k in known})


@dataclass
class RunConfig:
    symbol: str = "48704"
    timeframe: str = "1d"
    bar_minutes: int = 375
    intraday_only: bool = False
    session_start: str = "09:15"
    flatten_at: str = "15:10"
    entry_cutoff: str = "14:45"
    strategy: str = "donchian"
    params: dict[str, Any] = field(default_factory=dict)
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    def with_params(self, overrides: dict[str, Any]) -> "RunConfig":
        params = dict(self.params)
        params.update(overrides)
        return RunConfig(
            symbol=self.symbol,
            timeframe=self.timeframe,
            bar_minutes=self.bar_minutes,
            intraday_only=self.intraday_only,
            session_start=self.session_start,
            flatten_at=self.flatten_at,
            entry_cutoff=self.entry_cutoff,
            strategy=overrides.get("strategy", self.strategy),
            params=params,
            costs=self.costs,
            risk=self.risk,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_minutes": self.bar_minutes,
            "intraday_only": self.intraday_only,
            "session_start": self.session_start,
            "flatten_at": self.flatten_at,
            "entry_cutoff": self.entry_cutoff,
            "strategy": self.strategy,
            "params": dict(self.params),
            "costs": asdict(self.costs),
            "risk": asdict(self.risk),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunConfig":
        return cls(
            symbol=str(value.get("symbol", "48704")),
            timeframe=str(value.get("timeframe", "1d")),
            bar_minutes=int(value.get("bar_minutes", 375)),
            intraday_only=bool(value.get("intraday_only", False)),
            session_start=str(value.get("session_start", "09:15")),
            flatten_at=str(value.get("flatten_at", "15:10")),
            entry_cutoff=str(value.get("entry_cutoff", "14:45")),
            strategy=str(value.get("strategy", "donchian")),
            params=dict(value.get("params", {})),
            costs=CostConfig.from_dict(value.get("costs")),
            risk=RiskConfig.from_dict(value.get("risk")),
        )

    def to_json(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def load_run_config(path: str | Path) -> RunConfig:
    return RunConfig.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
