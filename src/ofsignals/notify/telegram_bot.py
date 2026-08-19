"""Telegram dispatch with an internal token bucket and retry/backoff.

The Bot API tolerates roughly 20 messages/minute to a single chat. We queue and
drain below that ceiling so a burst of signals never triggers a 429 cascade.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ofsignals.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class _Outbound:
    chat_id: str
    text: str
    silent: bool = False


class TelegramDispatcher:
    """Single-writer queue in front of the Bot API."""

    def __init__(self, token: str, chat_id: str, admin_chat_id: str = "",
                 max_per_minute: int = 18, parse_mode: str = "HTML",
                 retry_attempts: int = 4) -> None:
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

        self._bot = Bot(token=token)
        self._chat_id = chat_id
        self._admin_chat_id = admin_chat_id or chat_id
        self._parse_mode = ParseMode.HTML if parse_mode.upper() == "HTML" else ParseMode.MARKDOWN_V2
        self._retry_attempts = retry_attempts
        self._interval = 60.0 / max(max_per_minute, 1)
        self._queue: asyncio.Queue[_Outbound] = asyncio.Queue(maxsize=500)
        self._worker: asyncio.Task[None] | None = None
        self._last_sent = 0.0
        self.sent = 0
        self.dropped = 0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        me = await self._bot.get_me()
        self._worker = asyncio.create_task(self._drain(), name="telegram-drain")
        log.info("telegram_ready", bot=me.username, chat=self._chat_id)

    async def stop(self) -> None:
        if self._worker:
            await self._queue.join()
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        log.info("telegram_stopped", sent=self.sent, dropped=self.dropped)

    # ------------------------------------------------------------- enqueue
    async def send(self, text: str, *, admin: bool = False, silent: bool = False) -> None:
        item = _Outbound(
            chat_id=self._admin_chat_id if admin else self._chat_id,
            text=text,
            silent=silent,
        )
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning("telegram_queue_full", dropped=self.dropped)

    # -------------------------------------------------------------- worker
    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                gap = time.monotonic() - self._last_sent
                if gap < self._interval:
                    await asyncio.sleep(self._interval - gap)
                await self._deliver(item)
                self._last_sent = time.monotonic()
                self.sent += 1
            except Exception as exc:  # noqa: BLE001 - a failed send must not kill the loop
                self.dropped += 1
                log.error("telegram_send_failed", error=str(exc))
            finally:
                self._queue.task_done()

    async def _deliver(self, item: _Outbound) -> None:
        @retry(
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type(TelegramError),
            reraise=True,
        )
        async def _attempt() -> None:
            try:
                await self._bot.send_message(
                    chat_id=item.chat_id,
                    text=item.text,
                    parse_mode=self._parse_mode,
                    disable_web_page_preview=True,
                    disable_notification=item.silent,
                )
            except RetryAfter as exc:
                # Honour the server's own cooldown instead of guessing.
                await asyncio.sleep(float(exc.retry_after) + 0.5)
                raise

        await _attempt()
