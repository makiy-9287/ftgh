"""Swing engine: 1D bias / 4H liquidity / 1H confirm / 15M entry.

Swing setups hold through multiple sessions, so the footprint layer carries less
weight here — a 15-minute absorption print says little about a four-day thesis.
The mode compensates by demanding a higher confluence gate (78) and a 3.0 R:R
floor, and by rejecting setups whose daily structure is younger than a week.
"""

from __future__ import annotations

from typing import Any

from ofsignals.analytics.structure import compute_bias
from ofsignals.exchange.rest_client import CandleStore
from ofsignals.strategy.mtf import CascadeEngine, Evaluation, _reject

MIN_BIAS_AGE_BARS = 2      # daily structure must have had time to matter


class SwingEngine(CascadeEngine):
    mode_name = "swing"

    def __init__(self, settings: Any, candles: CandleStore, hub: Any) -> None:
        super().__init__("swing", settings, candles, hub)

    async def evaluate(self, symbol: str) -> Evaluation:
        daily = await self.candles.get(symbol, self.tf["bias"], limit=300)
        if len(daily) >= 60:
            bias = compute_bias(daily, self.structure_cfg)
            if bias.last_event is not None:
                age = len(daily) - 1 - bias.last_event.index
                if age < MIN_BIAS_AGE_BARS:
                    return _reject(symbol, self.mode, "L1",
                                   f"daily structure only {age} bar(s) old")
        return await super().evaluate(symbol)
