"""bot-telegram-alerts: Telegram alerts for bots, with dedup, rate limits and retries."""

from .alerter import (
    MAX_MESSAGE,
    SendResult,
    Stats,
    TelegramAlerter,
    TelegramError,
    escape_html,
    escape_markdown_v2,
    split_message,
)

__all__ = [
    "MAX_MESSAGE",
    "SendResult",
    "Stats",
    "TelegramAlerter",
    "TelegramError",
    "escape_html",
    "escape_markdown_v2",
    "split_message",
]
__version__ = "0.1.0"
