"""Telegram message rendering.

A signal card must be readable on a phone in five seconds: direction and pair
first, then the numbers you act on, then the reasoning for anyone who wants it.
Every price is formatted to the instrument's own scale — printing BTC to six
decimals or SHIB to two both make the card useless.
"""

from __future__ import annotations

import html

from ofsignals.store.db import OpenSignal
from ofsignals.strategy.base import Signal
from ofsignals.types import Direction

ARROW = {"LONG": "🟢 LONG", "SHORT": "🔴 SHORT"}
MODE_LABEL = {"scalp": "⚡ Scalp", "intraday": "📊 Day", "swing": "🌊 Swing"}


def fmt_price(value: float) -> str:
    """Adaptive precision: significant digits, not fixed decimals."""
    magnitude = abs(value)
    if magnitude == 0:
        return "0"
    if magnitude >= 1000:
        return f"{value:,.2f}"
    if magnitude >= 100:
        return f"{value:.3f}"
    if magnitude >= 1:
        return f"{value:.4f}"
    if magnitude >= 0.01:
        return f"{value:.6f}"
    return f"{value:.8f}"


def _pct(from_price: float, to_price: float) -> str:
    if from_price <= 0:
        return ""
    return f"{(to_price - from_price) / from_price * 100:+.2f}%"


def format_signal(signal: Signal) -> str:
    entry_high, entry_low = signal.entry_range
    mid = (entry_high + entry_low) / 2.0
    direction = signal.direction.value
    allocation = signal.tp_allocation

    lines = [
        f"{ARROW.get(direction, direction)}  <b>{html.escape(signal.symbol)}</b>",
        f"{MODE_LABEL.get(signal.mode, signal.mode)} · "
        f"score <b>{signal.confluence_score}</b>/100 · "
        f"{signal.leverage['recommended']}x isolated",
        "",
        f"<b>Entry</b>  <code>{fmt_price(entry_low)} – {fmt_price(entry_high)}</code>",
        f"<b>Stop</b>   <code>{fmt_price(signal.stop_loss)}</code>  "
        f"({signal.sl_distance_pct:.2f}%)",
        "",
        f"TP1  <code>{fmt_price(signal.targets['tp1'])}</code>  "
        f"{signal.rr['tp1']:.1f}R · {allocation['tp1'] * 100:.0f}%  {_pct(mid, signal.targets['tp1'])}",
        f"TP2  <code>{fmt_price(signal.targets['tp2'])}</code>  "
        f"{signal.rr['tp2']:.1f}R · {allocation['tp2'] * 100:.0f}%  {_pct(mid, signal.targets['tp2'])}",
        f"TP3  <code>{fmt_price(signal.targets['tp3'])}</code>  "
        f"{signal.rr['tp3']:.1f}R · {allocation['tp3'] * 100:.0f}%  {_pct(mid, signal.targets['tp3'])}",
        "",
        "<b>Why</b>",
    ]

    for key in ("bias", "liquidity", "flow", "dom", "entry", "invalidation"):
        text = signal.rationale.get(key)
        if text:
            lines.append(f"• <i>{key}</i>: {html.escape(text)}")

    tf = signal.timeframes
    lines += [
        "",
        f"<code>{tf['bias']}→{tf['liquidity']}→{tf['confirm']}→{tf['entry']}</code> · "
        f"max safe {signal.leverage['max_safe']}x",
        f"Valid until <code>{signal.valid_until}</code>",
    ]
    if signal.tags:
        lines.append(" · ".join(html.escape(tag) for tag in signal.tags))

    lines.append(f"<i>{signal.signal_id[:8]} · not financial advice</i>")
    return "\n".join(lines)


STATUS_TEXT = {
    "filled": ("🎯", "ENTRY FILLED", "position is live"),
    "tp1": ("✅", "TP1 HIT", "40% closed · stop moved to break-even"),
    "tp2": ("✅", "TP2 HIT", "35% closed · trailing the runner"),
    "tp3": ("🏁", "TP3 HIT", "runner closed · trade complete"),
    "stopped": ("🛑", "STOP LOSS HIT", "full position closed"),
    "closed_be": ("⚪", "CLOSED AT BREAK-EVEN", "remainder scratched"),
    "expired": ("⌛", "EXPIRED UNFILLED", "entry never traded — not chased"),
    "invalidated": ("🚫", "INVALIDATED BEFORE FILL", "setup voided pre-entry"),
}


def format_update(signal: OpenSignal, new_status: str, price: float,
                  r_multiple: float | None = None) -> str:
    """Alert for a status change. Coin name and event lead, so it is readable
    from a phone lock screen without opening the app."""
    icon, headline, detail = STATUS_TEXT.get(new_status, ("ℹ️", new_status.upper(), ""))
    side = "LONG" if signal.direction is Direction.LONG else "SHORT"
    pair = html.escape(signal.symbol.split("/")[0] + "USDT"
                       if "/" in signal.symbol else signal.symbol)

    parts = [
        f"{icon} <b>{pair} {headline}</b>",
        f"{side} · {signal.mode} · @ <code>{fmt_price(price)}</code>",
    ]
    if detail:
        parts.append(f"<i>{detail}</i>")
    if r_multiple is not None:
        parts.append(f"Booked: <b>{r_multiple:+.2f}R</b>")
    parts.append(f"<i>{signal.signal_id[:8]}</i>")
    return "\n".join(parts)


def format_heartbeat(stats: dict) -> str:
    return (
        "<b>ofsignals heartbeat</b>\n"
        f"Universe <code>{stats.get('universe', 0)}</code> · "
        f"fresh books <code>{stats.get('fresh_books', 0)}</code>\n"
        f"Scanned <code>{stats.get('evaluations', 0)}</code> · "
        f"published <code>{stats.get('published', 0)}</code> "
        f"(24h: <code>{stats.get('published_24h', 0)}</code>)\n"
        f"Open <code>{stats.get('open_signals', 0)}</code> · "
        f"cache hit rate <code>{stats.get('cache_hit_rate', '0%')}</code>"
    )


def format_rejection_digest(counts: dict[str, int], top: int = 6) -> str:
    """Where setups are dying. The most useful diagnostic in the whole system."""
    if not counts:
        return "<i>no evaluations yet</i>"
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]
    body = "\n".join(f"<code>{count:>5}</code>  {html.escape(stage)}"
                     for stage, count in ordered)
    return f"<b>Rejection stages (last cycle)</b>\n{body}"
