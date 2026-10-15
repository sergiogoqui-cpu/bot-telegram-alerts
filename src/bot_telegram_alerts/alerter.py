"""Telegram alerts that don't spam, don't flood the API and don't leak the bot token."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("bot_telegram_alerts")

MAX_MESSAGE = 4096  # Telegram's hard limit per message

Transport = Callable[[str, dict[str, Any]], "tuple[int, dict[str, Any]]"]


def escape_html(text: str) -> str:
    """Escape text for ``parse_mode="HTML"``."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_markdown_v2(text: str) -> str:
    """Escape text for ``parse_mode="MarkdownV2"``."""
    return _MDV2_SPECIAL.sub(r"\\\1", text)


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split on line boundaries so each chunk fits Telegram's limit (hard-cut very long lines)."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single line longer than the limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


@dataclass(frozen=True)
class SendResult:
    sent: bool
    reason: str = "ok"  # ok | duplicate | rate_limited | failed
    chunks: int = 0


@dataclass
class Stats:
    sent: int = 0
    duplicates: int = 0
    rate_limited: int = 0
    failed: int = 0
    retries: int = 0


class TelegramError(RuntimeError):
    """A send failed for good. The message never contains the bot token."""


def _urllib_transport(timeout: float) -> Transport:
    def post(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read() or b"{}")
            except ValueError:
                body = {}
            return exc.code, body

    return post


@dataclass
class TelegramAlerter:
    """Send alerts to a Telegram chat from a bot.

    - **Deduplication:** the same alert (same ``key``, default = hash of the text) is sent at most
      once per ``dedup_window`` seconds; the next one reports how many were suppressed.
    - **Rate limiting:** at most ``max_per_minute`` messages per rolling minute.
    - **Retries:** HTTP 429 (honours ``retry_after``), 5xx and network errors, with backoff.
    - **Long messages** are split on line boundaries.
    - The bot token is never logged and never appears in exceptions.
    """

    token: str = field(repr=False)
    chat_id: str | int
    dedup_window: float = 300.0
    max_per_minute: int = 20
    max_retries: int = 3
    on_limit: str = "drop"  # "drop" or "wait"
    timeout: float = 10.0
    transport: Transport | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    stats: Stats = field(default_factory=Stats)

    def __post_init__(self) -> None:
        if not self.token or not self.chat_id:
            raise ValueError("token and chat_id are required")
        if self.on_limit not in ("drop", "wait"):
            raise ValueError('on_limit must be "drop" or "wait"')
        self._transport = self.transport or _urllib_transport(self.timeout)
        self._seen: dict[str, tuple[float, int]] = {}  # key -> (last sent at, suppressed since)
        self._window: deque[float] = deque()

    # ------------------------------------------------------------------ public

    def send(
        self,
        text: str,
        *,
        key: str | None = None,
        parse_mode: str | None = None,
        silent: bool = False,
    ) -> SendResult:
        """Send ``text``. Returns a :class:`SendResult`; raises :class:`TelegramError` on failure."""
        now = self.clock()
        k = key or hashlib.sha256(text.encode()).hexdigest()[:16]
        last = self._seen.get(k)
        if last and now - last[0] < self.dedup_window:
            self._seen[k] = (last[0], last[1] + 1)
            self.stats.duplicates += 1
            return SendResult(False, "duplicate")
        suppressed = last[1] if last else 0

        if not self._acquire_slot():
            self.stats.rate_limited += 1
            return SendResult(False, "rate_limited")

        body = f"{text}\n(+{suppressed} repeated)" if suppressed else text
        chunks = split_message(body)
        for chunk in chunks:
            self._post(chunk, parse_mode, silent)
        self._seen[k] = (self.clock(), 0)
        self.stats.sent += 1
        return SendResult(True, "ok", len(chunks))

    def send_safe(self, text: str, **kwargs: Any) -> SendResult:
        """Like :meth:`send` but never raises: alerting must not crash the bot it watches."""
        try:
            return self.send(text, **kwargs)
        except TelegramError as exc:
            log.error("alert failed: %s", exc)
            return SendResult(False, "failed")

    # ---------------------------------------------------------------- internals

    def _acquire_slot(self) -> bool:
        while True:
            now = self.clock()
            while self._window and now - self._window[0] >= 60:
                self._window.popleft()
            if len(self._window) < self.max_per_minute:
                self._window.append(now)
                return True
            if self.on_limit == "drop":
                return False
            self.sleep(max(60 - (now - self._window[0]), 0.01))

    def _post(self, text: str, parse_mode: str | None, silent: bool) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        delay = 1.0
        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                status, body = self._transport(url, payload)
            except Exception as exc:  # noqa: BLE001 - network layer errors
                status, body = 0, {}
                last_error = type(exc).__name__ + ": " + str(exc).replace(self.token, "***")
            else:
                if status == 200 and body.get("ok", True):
                    return
                last_error = f"HTTP {status}: {body.get('description', 'no description')}"
            retryable = status in (0, 429) or status >= 500
            if not retryable or attempt == self.max_retries:
                self.stats.failed += 1
                raise TelegramError(last_error.replace(self.token, "***"))
            self.stats.retries += 1
            wait = float(body.get("parameters", {}).get("retry_after", delay)) if status == 429 else delay
            self.sleep(wait)
            delay = min(delay * 2, 30.0)
