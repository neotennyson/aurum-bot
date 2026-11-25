"""Indian F&O transaction cost model.

Futures defaults (NSE, 2026): brokerage Rs.20 or 0.03% per order (whichever is
lower), exchange transaction charge ~0.0019%, STT 0.02% on the sell side,
stamp duty 0.002% on the buy side, SEBI fee Rs.10/crore, GST 18% on brokerage
+ exchange + SEBI. Slippage is modelled as bps on the reference price.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import CostConfig


@dataclass(frozen=True)
class Charges:
    brokerage: float
    exchange: float
    stt: float
    stamp: float
    sebi: float
    gst: float

    @property
    def total(self) -> float:
        return self.brokerage + self.exchange + self.stt + self.stamp + self.sebi + self.gst


class CostModel:
    def __init__(self, config: CostConfig | None = None) -> None:
        self.config = config or CostConfig()

    def fill_price(self, reference: float, buying: bool) -> float:
        slip = self.config.slippage_bps / 10_000.0
        return reference * (1.0 + slip) if buying else reference * (1.0 - slip)

    def charges(self, buying: bool, price: float, quantity: int, multiplier: float) -> Charges:
        turnover = abs(price * quantity * multiplier)
        brokerage = min(self.config.brokerage_per_order, turnover * self.config.brokerage_pct)
        exchange = turnover * self.config.exchange_pct
        stt = 0.0 if buying else turnover * self.config.stt_sell_pct
        stamp = turnover * self.config.stamp_buy_pct if buying else 0.0
        sebi = turnover * self.config.sebi_pct
        gst = (brokerage + exchange + sebi) * self.config.gst_pct
        return Charges(brokerage, exchange, stt, stamp, sebi, gst)
