"""Signal payload and the confluence scorer.

The score is published with every signal and persisted with its full breakdown.
That is the only way the gates ever get tuned by evidence instead of by feel:
after a few hundred signals you bucket outcomes by score band and find out
whether 72-79 actually deserves to pass.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from ofsignals.analytics.orderbook import dom_confirms
from ofsignals.risk.sizing import TradePlan
from ofsignals.types import (
    Bias,
    BookSnapshot,
    Direction,
    FlowSnapshot,
    Sweep,
    VolumeProfile,
    Zone,
    ZoneKind,
    iso,
    utcnow,
)

LONDON_OPEN_H, NY_OPEN_H = 7, 13
SESSION_WINDOW_MINUTES = 90


@dataclass(slots=True)
class ScoreCard:
    bias: int = 0
    sweep: int = 0
    poi: int = 0
    flow: int = 0
    dom: int = 0
    vp: int = 0
    session: int = 0
    rr: int = 0

    @property
    def total(self) -> int:
        return (self.bias + self.sweep + self.poi + self.flow
                + self.dom + self.vp + self.session + self.rr)

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(slots=True)
class Signal:
    signal_id: str
    generated_at: str
    symbol: str
    direction: Direction
    mode: str
    timeframes: dict[str, str]
    entry_range: tuple[float, float]
    stop_loss: float
    targets: dict[str, float]
    tp_allocation: dict[str, float]
    rr: dict[str, float]
    leverage: dict[str, object]
    sl_distance_pct: float
    confluence_score: int
    score_breakdown: dict[str, int]
    rationale: dict[str, str]
    valid_until: str
    price_at_signal: float
    status: str = "pending"
    tags: list[str] = field(default_factory=list)

    @property
    def dedupe_key(self) -> str:
        return f"{self.symbol}:{self.mode}:{self.direction.value}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["direction"] = self.direction.value
        data["entry_range"] = list(self.entry_range)
        return data


def build_signal(symbol: str, mode: str, direction: Direction, timeframes: dict[str, str],
                 plan: TradePlan, score: ScoreCard, rationale: dict[str, str],
                 price: float, ttl_minutes: int, allocation: dict[str, float],
                 tags: list[str] | None = None) -> Signal:
    now = utcnow()
    return Signal(
        signal_id=str(uuid.uuid4()),
        generated_at=iso(now),
        symbol=symbol,
        direction=direction,
        mode=mode,
        timeframes=dict(timeframes),
        entry_range=(plan.entry_high, plan.entry_low),
        stop_loss=plan.stop_loss,
        targets={"tp1": plan.tp1, "tp2": plan.tp2, "tp3": plan.tp3},
        tp_allocation=dict(allocation),
        rr={"tp1": plan.rr1, "tp2": plan.rr2, "tp3": plan.rr3},
        leverage={"recommended": plan.leverage, "max_safe": plan.leverage_max_safe,
                  "margin_mode": "isolated"},
        sl_distance_pct=plan.sl_distance_pct,
        confluence_score=score.total,
        score_breakdown=score.as_dict(),
        rationale=rationale,
        valid_until=iso(now + timedelta(minutes=ttl_minutes)),
        price_at_signal=round(price, 10),
        tags=tags or [],
    )


# ---------------------------------------------------------------------- scorer
def score_setup(bias: Bias, in_correct_half: bool, sweep: Sweep, zone: Zone,
                fvg_overlap: bool, flow: FlowSnapshot, book: BookSnapshot,
                profile: VolumeProfile | None, entry_at_lvn: bool,
                naked_poc_target: bool, plan: TradePlan, min_rr: float,
                orderflow_cfg: dict, sweep_volume_floor: float,
                weekend: bool = False) -> ScoreCard:
    card = ScoreCard()

    # -- bias alignment + premium/discount position ----------------------- /20
    if bias.tradeable:
        card.bias = 20 if in_correct_half else 10

    # -- sweep quality ---------------------------------------------------- /20
    if sweep.volume_multiple >= sweep_volume_floor:
        card.sweep = 20
    elif sweep.volume_multiple >= sweep_volume_floor * 0.83:
        card.sweep = 12

    # -- POI quality ------------------------------------------------------ /15
    if zone.fresh and fvg_overlap:
        card.poi = 15
    elif zone.fresh or zone.kind is ZoneKind.ORDER_BLOCK:
        card.poi = 8

    # -- order flow ------------------------------------------------------- /15
    if flow.ready:
        if flow.cvd_divergence and flow.absorption:
            card.flow = 15
        elif flow.cvd_divergence or flow.absorption:
            card.flow = 9

    # -- DOM -------------------------------------------------------------- /10
    _, card.dom = dom_confirms(book, plan.direction, orderflow_cfg)

    # -- volume profile context ------------------------------------------- /10
    if profile is not None:
        if entry_at_lvn and naked_poc_target:
            card.vp = 10
        elif entry_at_lvn or naked_poc_target:
            card.vp = 6
        elif not profile.in_value_area(plan.entry_mid):
            card.vp = 3

    # -- session timing ---------------------------------------------------- /5
    now = utcnow()
    minutes = now.hour * 60 + now.minute
    for open_hour in (LONDON_OPEN_H, NY_OPEN_H):
        if abs(minutes - open_hour * 60) <= SESSION_WINDOW_MINUTES:
            card.session = 5
            break

    # -- reward quality ---------------------------------------------------- /5
    if plan.rr2 >= min_rr * 1.5:
        card.rr = 5
    elif plan.rr2 >= min_rr * 1.2:
        card.rr = 2

    # NOTE: the weekend penalty is applied ONCE, by raising the gate in
    # `effective_gate`. Deducting points here as well would charge it twice.
    return card


def effective_gate(base_gate: int, weekend: bool) -> int:
    return base_gate + 5 if weekend else base_gate
