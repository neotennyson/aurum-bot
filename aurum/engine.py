"""Causal event-driven backtest engine.

Rules that make the simulation honest:
* Signals are computed on closed bars and filled at the next bar's open.
* Slippage is applied against the trade direction; gaps through stops fill at
  the open, not at the stop price.
* Stops are checked before targets inside a bar.
* Every rupee of fees is charged; final equity always reconciles to the sum
  of trade P&L plus starting capital.
"""

from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Any

from .bars import Bar
from .config import RunConfig
from .costs import CostModel
from .models import BacktestResult, EquityPoint, Position, Side, Signal, Trade, parse_side
from .risk import RiskManager
from .strategies import make_strategy


class BacktestEngine:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.strategy = make_strategy(config)
        self.costs = CostModel(config.costs)
        self.risk = RiskManager(config.risk)

    def run(self, bars: list[Bar], trade_from: Any | None = None) -> BacktestResult:
        """Run the engine over ``bars``.

        ``trade_from`` may be a timestamp: bars before it only warm up
        indicators, no position or equity point exists before it. This keeps
        walk-forward test windows honest without starving the strategy of
        history.
        """
        data = sorted((bar for bar in bars if bar.symbol == self.config.symbol), key=lambda bar: bar.timestamp)
        if len(data) < self.strategy.warmup:
            raise ValueError(
                f"need at least {self.strategy.warmup} bars for strategy {self.strategy.name!r}, got {len(data)}"
            )
        capital = self.config.risk.capital
        cash = capital
        position: Position | None = None
        pending: dict[str, Any] | None = None
        trades: list[Trade] = []
        signals: list[Signal] = []
        curve: list[EquityPoint] = []
        peak = capital

        for index, bar in enumerate(data):
            if trade_from is not None and bar.timestamp < trade_from:
                continue
            equity_open = cash if position is None else cash + position.unrealized(bar.open) - position.entry_fee
            self.risk.start_day(bar.timestamp, equity_open)

            if pending is not None:
                if pending["kind"] == "enter" and position is None:
                    position = self._open(pending["signal"], bar, index, equity_open)
                elif pending["kind"] == "exit" and position is not None:
                    cash = self._close(position, bar.open, bar.timestamp, index, pending["reason"], cash, trades)
                    position = None
                pending = None

            if position is not None:
                position, cash = self._intrabar_exit(position, bar, index, cash, trades)
                if position is not None:
                    position.bars_held += 1
                    if position.max_holding_bars and position.bars_held >= position.max_holding_bars:
                        cash = self._close(position, bar.close, bar.timestamp, index, "max_holding", cash, trades)
                        position = None

            equity = cash if position is None else cash + position.unrealized(bar.close) - position.entry_fee
            if position is not None and self.risk.daily_loss_breached(bar.timestamp, equity):
                cash = self._close(position, bar.close, bar.timestamp, index, "daily_loss_halt", cash, trades)
                position = None
                equity = cash

            peak = max(peak, equity)
            drawdown = equity / peak - 1.0 if peak else 0.0
            curve.append(
                EquityPoint(
                    timestamp=bar.timestamp,
                    equity=equity,
                    drawdown=drawdown,
                    price=bar.close,
                    position=position.quantity if position else 0,
                )
            )

            if index == len(data) - 1:
                if position is not None:
                    cash = self._close(position, bar.close, bar.timestamp, index, "end_of_data", cash, trades)
                    position = None
                    peak = max(peak, cash)
                    curve[-1] = EquityPoint(
                        timestamp=bar.timestamp,
                        equity=cash,
                        drawdown=cash / peak - 1.0 if peak else 0.0,
                        price=bar.close,
                        position=0,
                    )
                break

            history = data[max(0, index + 1 - self.strategy.history_depth) : index + 1]
            if position is None and pending is None:
                signal = self.strategy.signal(history)
                if signal is not None and self.risk.can_enter(
                    bar.timestamp, equity, bar.close, signal.stop_distance, index
                ):
                    signals.append(signal)
                    pending = {"kind": "enter", "signal": signal}
            elif position is not None and pending is None:
                if self.strategy.should_flatten(bar):
                    pending = {"kind": "exit", "reason": "session_flatten"}
                elif self.strategy.exit_signal(position, history):
                    pending = {"kind": "exit", "reason": "strategy_exit"}
                else:
                    signal = self.strategy.signal(history)
                    if signal is not None and signal.side is not position.side:
                        pending = {"kind": "exit", "reason": "opposing_signal"}

        metrics = performance_metrics(curve, trades, capital, self.config.bar_minutes)
        return BacktestResult(
            symbol=self.config.symbol,
            initial_capital=capital,
            final_equity=cash,
            metrics=metrics,
            trades=trades,
            equity_curve=curve,
            signals=signals,
            config=self.config.to_dict(),
        )

    # -- order handling ----------------------------------------------------
    def _open(self, signal: Signal, bar: Bar, index: int, equity: float) -> Position | None:
        buying = signal.side is Side.LONG
        entry_price = self.costs.fill_price(bar.open, buying)
        decision = self.risk.size(equity, entry_price, signal.stop_distance, signal.confidence)
        if decision.quantity < 1:
            return None
        entry_fee = self.costs.charges(buying, entry_price, decision.quantity, self.config.risk.multiplier).total
        self.risk.register_entry(index)
        stop = entry_price - signal.side.sign * signal.stop_distance
        target = entry_price + signal.side.sign * signal.stop_distance * signal.target_r
        return Position(
            symbol=signal.symbol,
            side=signal.side,
            quantity=decision.quantity,
            entry_price=entry_price,
            entry_time=bar.timestamp,
            stop_price=stop,
            target_price=target,
            entry_fee=entry_fee,
            multiplier=self.config.risk.multiplier,
            margin=decision.margin,
            max_holding_bars=self.strategy.max_holding_bars,
            reason=signal.reason,
        )

    def _intrabar_exit(
        self, position: Position, bar: Bar, index: int, cash: float, trades: list[Trade]
    ) -> tuple[Position | None, float]:
        long = position.side is Side.LONG
        if long:
            if bar.open <= position.stop_price:
                return None, self._close(position, bar.open, bar.timestamp, index, "gap_stop", cash, trades)
            if bar.low <= position.stop_price:
                return None, self._close(position, position.stop_price, bar.timestamp, index, "stop_loss", cash, trades)
            if bar.open >= position.target_price:
                return None, self._close(position, bar.open, bar.timestamp, index, "gap_target", cash, trades)
            if bar.high >= position.target_price:
                return None, self._close(position, position.target_price, bar.timestamp, index, "take_profit", cash, trades)
        else:
            if bar.open >= position.stop_price:
                return None, self._close(position, bar.open, bar.timestamp, index, "gap_stop", cash, trades)
            if bar.high >= position.stop_price:
                return None, self._close(position, position.stop_price, bar.timestamp, index, "stop_loss", cash, trades)
            if bar.open <= position.target_price:
                return None, self._close(position, bar.open, bar.timestamp, index, "gap_target", cash, trades)
            if bar.low <= position.target_price:
                return None, self._close(position, position.target_price, bar.timestamp, index, "take_profit", cash, trades)
        return position, cash

    def _close(
        self,
        position: Position,
        reference: float,
        timestamp: Any,
        index: int,
        reason: str,
        cash: float,
        trades: list[Trade],
    ) -> float:
        closing_buy = position.side is Side.SHORT
        exit_price = self.costs.fill_price(reference, closing_buy)
        exit_fee = self.costs.charges(closing_buy, exit_price, position.quantity, position.multiplier).total
        gross = (exit_price - position.entry_price) * position.side.sign * position.quantity * position.multiplier
        net = gross - position.entry_fee - exit_fee
        cash += gross - position.entry_fee - exit_fee
        trades.append(
            Trade(
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
        )
        self.risk.register_exit(net, index)
        return cash


def periods_per_year(bar_minutes: int) -> float:
    if bar_minutes >= 375:
        return 252.0
    return 252.0 * 375.0 / max(bar_minutes, 1)


def performance_metrics(
    curve: list[EquityPoint],
    trades: list[Trade],
    initial_capital: float,
    bar_minutes: int = 375,
) -> dict[str, Any]:
    if not curve:
        return {"trades": 0, "status": "insufficient_data"}
    values = [point.equity for point in curve]
    returns = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]
    final_equity = values[-1]
    total_return = final_equity / initial_capital - 1.0 if initial_capital else 0.0
    days = max((curve[-1].timestamp - curve[0].timestamp).total_seconds() / 86400.0, 1.0)
    years = days / 365.25
    cagr = (final_equity / initial_capital) ** (1.0 / years) - 1.0 if final_equity > 0 and initial_capital > 0 else -1.0
    ppy = periods_per_year(bar_minutes)
    volatility = pstdev(returns) * math.sqrt(ppy) if len(returns) > 1 else 0.0
    if len(returns) > 1 and pstdev(returns) > 0:
        sharpe = mean(returns) / pstdev(returns) * math.sqrt(ppy)
    else:
        sharpe = 0.0
    downside = [min(value, 0.0) for value in returns]
    downside_dev = math.sqrt(mean([value * value for value in downside])) if downside else 0.0
    sortino = mean(returns) / downside_dev * math.sqrt(ppy) if downside_dev > 0 else 0.0
    peak = initial_capital
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1.0)
    daily: dict[Any, float] = {}
    for point in curve:
        daily[point.timestamp.date()] = point.equity
    daily_returns = []
    previous = initial_capital
    for value in daily.values():
        if previous > 0:
            daily_returns.append(value / previous - 1.0)
        previous = value
    winners = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
    losers = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    monthly: dict[str, float] = {}
    month_end: dict[str, float] = {}
    for point in curve:
        month_end[point.timestamp.strftime("%Y-%m")] = point.equity
    previous_month = initial_capital
    for key in sorted(month_end):
        monthly[key] = month_end[key] / previous_month - 1.0 if previous_month else 0.0
        previous_month = month_end[key]
    exit_reasons: dict[str, int] = {}
    for trade in trades:
        exit_reasons[trade.exit_reason] = exit_reasons.get(trade.exit_reason, 0) + 1
    return {
        "total_return": total_return,
        "cagr": cagr,
        "annualized_volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "calmar": cagr / abs(max_drawdown) if max_drawdown < 0 else 0.0,
        "win_rate": len(winners) / len(trades) if trades else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "expectancy": mean([trade.net_pnl for trade in trades]) if trades else 0.0,
        "average_win": mean(winners) if winners else 0.0,
        "average_loss": mean(losers) if losers else 0.0,
        "trades": len(trades),
        "long_trades": sum(1 for trade in trades if trade.side is Side.LONG),
        "short_trades": sum(1 for trade in trades if trade.side is Side.SHORT),
        "total_fees": sum(trade.fees for trade in trades),
        "exposure_fraction": sum(1 for point in curve if point.position != 0) / len(curve),
        "average_holding_bars": mean([trade.bars_held for trade in trades]) if trades else 0.0,
        "benchmark_return": curve[-1].price / curve[0].price - 1.0 if curve[0].price else 0.0,
        "best_day": max(daily_returns) if daily_returns else 0.0,
        "worst_day": min(daily_returns) if daily_returns else 0.0,
        "monthly_returns": monthly,
        "exit_reasons": exit_reasons,
        "start_equity": values[0],
        "final_equity": final_equity,
    }


__all__ = ["BacktestEngine", "performance_metrics", "parse_side"]
