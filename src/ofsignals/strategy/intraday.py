"""Day-trading engine: 4H bias / 1H liquidity / 15M confirm / 5M entry.

The middle mode runs the cascade unmodified. Its selectivity comes from the
config ladder — a 2.5 R:R floor and a 75 gate — rather than extra rules.
"""

from __future__ import annotations

from typing import Any

from ofsignals.exchange.rest_client import CandleStore
from ofsignals.strategy.mtf import CascadeEngine


class IntradayEngine(CascadeEngine):
    mode_name = "intraday"

    def __init__(self, settings: Any, candles: CandleStore, hub: Any) -> None:
        super().__init__("intraday", settings, candles, hub)
