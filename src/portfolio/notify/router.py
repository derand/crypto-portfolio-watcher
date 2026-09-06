"""Fan-out to enabled channels.

Phase 0 keeps this deliberately thin: build the channel list, send to all,
never let one dead channel stop the others. Thresholds, batching and
severity-based routing land in phase 9, all behind this same call.
"""

import asyncio
import logging

from ..models import Message
from ..retry import with_retry
from .telegram import TelegramNotifier

log = logging.getLogger(__name__)


def build_notifiers(cfg) -> list:
    out = []
    if cfg.notify.telegram.enabled:
        out.append(TelegramNotifier(cfg.notify.telegram.bot_token,
                                    cfg.notify.telegram.chat_id))
    # ntfy and discord: phase 9
    return out


class Router:
    def __init__(self, notifiers: list):
        self._notifiers = notifiers

    @property
    def channels(self) -> list[str]:
        return [n.name for n in self._notifiers]

    async def send(self, msg: Message) -> dict[str, str | None]:
        """Returns {channel: None on success | error string}.

        Failures are reported, not raised: a broken Discord webhook must not
        suppress the Telegram alert about the money.
        """
        async def one(n):
            try:
                await with_retry(lambda: n.send(msg), what=f"{n.name} send")
                return n.name, None
            except Exception as e:  # noqa: BLE001
                log.error("%s delivery failed: %s", n.name, e)
                return n.name, str(e)

        results = await asyncio.gather(*(one(n) for n in self._notifiers))
        return dict(results)
