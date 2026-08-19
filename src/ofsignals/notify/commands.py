"""Telegram command interface.

Runs a polling `Application` alongside the outbound dispatcher. Commands are
read-only views onto live engine state plus the signal journal — nothing here
can place an order or change strategy parameters, by design.

Access is restricted to the configured chat and admin chat. An open bot is an
open window onto your positions.
"""

from __future__ import annotations

import html
import time
from typing import Any, Callable

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from ofsignals.logging_setup import get_logger
from ofsignals.notify.formatter import fmt_price
from ofsignals.types import Direction

log = get_logger(__name__)

COMMANDS: list[tuple[str, str]] = [
    ("status", "engine health, warmup and scan stats"),
    ("active", "open signals with live PnL"),
    ("pnl", "realised + unrealised R summary"),
    ("watchlist", "pairs currently being scanned"),
    ("report", "outcomes by confluence score band"),
    ("why", "where setups are being rejected"),
    ("scan", "force an immediate scan pass"),
    ("help", "this list"),
]


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


class CommandServer:
    """Polling command handler bound to a running Engine."""

    def __init__(self, token: str, engine: Any, allowed_chat_ids: set[str]) -> None:
        self._token = token
        self._engine = engine
        self._allowed = {str(c) for c in allowed_chat_ids if c}
        self._app: Application | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._app = Application.builder().token(self._token).build()
        for name, _ in COMMANDS:
            self._app.add_handler(CommandHandler(name, getattr(self, f"_cmd_{name}")))
        self._app.add_handler(CommandHandler("start", self._cmd_help))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        try:
            await self._app.bot.set_my_commands(
                [BotCommand(name, desc) for name, desc in COMMANDS])
        except Exception as exc:  # noqa: BLE001 - cosmetic only
            log.debug("set_my_commands_failed", error=str(exc)[:120])
        log.info("commands_ready", count=len(COMMANDS))

    async def stop(self) -> None:
        if not self._app:
            return
        try:
            if self._app.updater.running:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.debug("commands_stop_error", error=str(exc)[:120])

    # -------------------------------------------------------------- plumbing
    async def _guard(self, update: Update) -> bool:
        chat = update.effective_chat
        if chat is None or str(chat.id) not in self._allowed:
            log.warning("command_rejected", chat_id=getattr(chat, "id", None))
            return False
        return True

    async def _reply(self, update: Update, text: str) -> None:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    def _price_of(self, symbol: str) -> float | None:
        state = self._engine.hub.book_state(symbol) if self._engine.hub else None
        if state and state.mid > 0:
            return state.mid
        return self._engine.last_prices.get(symbol)

    # -------------------------------------------------------------- commands
    async def _cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        lines = ["<b>Commands</b>"]
        lines += [f"/{name} — {desc}" for name, desc in COMMANDS]
        await self._reply(update, "\n".join(lines))

    async def _cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        engine = self._engine
        scanner, hub = engine.scanner, engine.hub
        hub_stats = hub.stats() if hub else {}

        ready, total = 0, 0
        for mode, cascade in (scanner.engines.items() if scanner else []):
            for info in (scanner.universe or [])[:200]:
                tape = hub.tape(info.symbol) if hub else None
                if tape is None:
                    continue
                total += 1
                bars, need = tape.readiness(cascade.footprint_bucket_ms,
                                            cascade.min_ready_bars)
                if bars >= need:
                    ready += 1
            break   # readiness is per-symbol; one mode is representative

        open_signals = await engine.store.open_signals()
        lines = [
            "<b>Engine status</b>",
            f"Uptime <code>{_fmt_duration(time.time() - engine.started_at)}</code> · "
            f"v{engine.version}",
            f"Universe <code>{len(scanner.universe) if scanner else 0}</code> · "
            f"fresh books <code>{hub_stats.get('fresh_books', 0)}</code>",
            f"Tape ready <code>{ready}/{total or '-'}</code> symbols",
            f"Evaluations <code>{scanner.evaluations if scanner else 0}</code> · "
            f"published <code>{scanner.published if scanner else 0}</code>",
            f"Open signals <code>{len(open_signals)}</code> · "
            f"24h <code>{await engine.store.count_since(1440)}</code>",
            f"Modes <code>{', '.join(engine.settings.enabled_modes)}</code>",
            f"Scan every <code>{engine.scan_interval:.0f}s</code>",
        ]
        if ready < total:
            lines.append("\n<i>Symbols still warming up cannot signal — the "
                         "footprint layer refuses to score invented flow.</i>")
        await self._reply(update, "\n".join(lines))

    async def _cmd_active(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        rows = await self._engine.store.open_signals()
        if not rows:
            await self._reply(update, "<i>No open signals.</i>")
            return

        lines = [f"<b>Active signals ({len(rows)})</b>"]
        for sig in rows:
            price = self._price_of(sig.symbol)
            entry = (sig.entry_high + sig.entry_low) / 2.0
            side = "🟢" if sig.direction is Direction.LONG else "🔴"
            head = (f"\n{side} <b>{html.escape(sig.symbol)}</b> {sig.mode} · "
                    f"<code>{sig.status}</code>")
            lines.append(head)
            if price is None:
                lines.append("  <i>no live price</i>")
                continue

            move = (price - entry) * sig.direction.sign
            r = move / sig.risk if sig.risk > 0 else 0.0
            pct = move / entry * 100 if entry else 0.0
            arrow = "▲" if move >= 0 else "▼"
            lines.append(
                f"  {arrow} <code>{fmt_price(price)}</code> · "
                f"<b>{r:+.2f}R</b> ({pct:+.2f}%)")
            lines.append(
                f"  entry <code>{fmt_price(entry)}</code> · "
                f"SL <code>{fmt_price(sig.stop_loss)}</code> · "
                f"TP <code>{fmt_price(sig.tp1)}/{fmt_price(sig.tp2)}/"
                f"{fmt_price(sig.tp3)}</code>")
        await self._reply(update, "\n".join(lines))

    async def _cmd_pnl(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        store = self._engine.store
        days = _int_arg(_ctx, default=30)
        realised = await store.realised_summary(days)
        rows = await store.open_signals()

        unrealised = 0.0
        for sig in rows:
            price = self._price_of(sig.symbol)
            if price is None or sig.risk <= 0:
                continue
            unrealised += (price - (sig.entry_high + sig.entry_low) / 2.0) \
                * sig.direction.sign / sig.risk

        wins, losses = realised["wins"], realised["losses"]
        resolved = wins + losses
        win_rate = (wins / resolved * 100) if resolved else 0.0

        lines = [
            f"<b>PnL — last {days}d</b>",
            f"Resolved <code>{resolved}</code> · "
            f"win rate <code>{win_rate:.0f}%</code> ({wins}W / {losses}L)",
            f"Realised <b>{realised['realised_r']:+.2f}R</b>",
            f"Open <code>{len(rows)}</code> · unrealised <b>{unrealised:+.2f}R</b>",
            f"Net <b>{realised['realised_r'] + unrealised:+.2f}R</b>",
            "",
            "<i>R is risk-multiple before fees. At 0.045% taker, round-trip "
            "costs eat roughly a third of 1R on a tight scalp stop.</i>",
        ]
        await self._reply(update, "\n".join(lines))

    async def _cmd_watchlist(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        universe = (self._engine.scanner.universe if self._engine.scanner else [])
        if not universe:
            await self._reply(update, "<i>Universe not built yet.</i>")
            return

        lines = [f"<b>Watchlist ({len(universe)})</b>",
                 "<code>pair          vol24h  spr  tape</code>"]
        hub = self._engine.hub
        cascade = next(iter(self._engine.scanner.engines.values()), None)
        for info in universe[:25]:
            tape = hub.tape(info.symbol) if hub else None
            if tape and cascade:
                bars, need = tape.readiness(cascade.footprint_bucket_ms,
                                            cascade.min_ready_bars)
                mark = "ok" if bars >= need else f"{bars}/{need}"
            else:
                mark = "-"
            lines.append(
                f"<code>{info.display:<12} {info.quote_volume_24h / 1e6:>6.0f}M "
                f"{info.spread_bps:>4.1f} {mark:>6}</code>")
        if len(universe) > 25:
            lines.append(f"<i>…and {len(universe) - 25} more</i>")
        await self._reply(update, "\n".join(lines))

    async def _cmd_report(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        days = _int_arg(_ctx, default=30)
        bands = await self._engine.store.performance(days)
        if not bands:
            await self._reply(
                update,
                f"<i>No resolved signals in the last {days}d yet. "
                f"The score bands only become meaningful after ~200.</i>")
            return

        lines = [f"<b>Outcomes by score band — {days}d</b>",
                 "<code>band     n   win%   MFE   MAE</code>"]
        for band, row in bands.items():
            n = row["n"] or 0
            wins = row["wins"] or 0
            rate = (wins / n * 100) if n else 0
            lines.append(
                f"<code>{band:<6} {n:>3} {rate:>5.0f}% "
                f"{row['avg_mfe'] or 0:>5.1f} {row['avg_mae'] or 0:>5.1f}</code>")
        lines.append("\n<i>If the lowest band does not underperform the highest, "
                     "the gate is not doing any work — raise it.</i>")
        await self._reply(update, "\n".join(lines))

    async def _cmd_why(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        scanner = self._engine.scanner
        detail = scanner.rejection_summary() if scanner else {}
        if not detail:
            await self._reply(update, "<i>No completed scan yet.</i>")
            return

        stages = scanner.stage_histogram()
        lines = ["<b>Why no signals — last scan</b>", "<i>by stage</i>"]
        for stage, count in sorted(stages.items(), key=lambda kv: -kv[1]):
            lines.append(f"<code>{count:>4}</code>  {html.escape(stage)}")
        lines.append("\n<i>top reasons</i>")
        for reason, count in sorted(detail.items(), key=lambda kv: -kv[1])[:6]:
            lines.append(f"<code>{count:>4}</code>  {html.escape(reason[:70])}")
        lines.append("\n<i>Most setups dying at L2 is normal — a sweep with "
                     "reclaim AND displacement is genuinely rare.</i>")
        await self._reply(update, "\n".join(lines))

    async def _cmd_scan(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        self._engine.request_scan()
        await self._reply(update, "🔍 <i>Scan queued — results within a few seconds.</i>")


def _int_arg(ctx: ContextTypes.DEFAULT_TYPE, default: int) -> int:
    try:
        return max(1, min(365, int(ctx.args[0])))
    except (IndexError, ValueError, TypeError, AttributeError):
        return default
