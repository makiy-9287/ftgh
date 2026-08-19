"""Scalping engine: 1H bias / 15M liquidity / 5M confirm / 3M entry.

Mode-specific behaviour beyond the config ladder: scalps are the only mode
sensitive to funding settlement, because a 0.8% stop and a funding-driven wick
occupy the same order of magnitude.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ofsignals.exchange.rest_client import CandleStore
from ofsignals.strategy.mtf import CascadeEngine, Evaluation, _reject
from ofsignals.types import utcnow

FUNDING_HOURS = (0, 8, 16)


class ScalpEngine(CascadeEngine):
    mode_name = "scalp"

    def __init__(self, settings: Any, candles: CandleStore, hub: Any) -> None:
        super().__init__("scalp", settings, candles, hub)
        self._funding_guard_minutes = int(
            settings.section("filters").get("avoid_funding_window_minutes", 3)
        )

    async def evaluate(self, symbol: str) -> Evaluation:
        if self._in_funding_window():
            return _reject(symbol, self.mode, "filter",
                           "inside funding settlement window")
        return await super().evaluate(symbol)

    def _in_funding_window(self) -> bool:
        now = utcnow()
        guard = timedelta(minutes=self._funding_guard_minutes)
        for hour in FUNDING_HOURS:
            boundary = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            for candidate in (boundary, boundary + timedelta(days=1),
                              boundary - timedelta(days=1)):
                if abs(now - candidate) <= guard:
                    return True
        return False
