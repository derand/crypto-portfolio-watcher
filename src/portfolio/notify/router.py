"""Fan-out to enabled channels.

Phase 0 keeps this deliberately thin: build the channel list, send to all,
never let one dead channel stop the others. Thresholds, batching and
severity-based routing land in phase 9, all behind this same call.
"""

import asyncio
import logging

from ..models import Message
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
        self._by_name = {n.name: n for n in notifiers}

    @property
    def channels(self) -> list[str]:
        return [n.name for n in self._notifiers]

    async def send_to(self, channel: str, msg: Message) -> str | None:
        """One channel. None on success, the error text on failure.

        The tick needs this because channels do not fall behind together: each
        owes its own set of events, and one that is broken must not cost the
        others a repeat of what they already took.

        No retry here on purpose: a Message may be several requests, and only
        the notifier knows where the boundaries are. Retrying at this level
        re-sent the parts that had already arrived.
        """
        notifier = self._by_name.get(channel)
        if notifier is None:
            return f"no channel named {channel}"
        try:
            await notifier.send(msg)
            return None
        except Exception as e:  # noqa: BLE001
            log.error("%s delivery failed: %s", channel, e)
            return str(e)

    async def send(self, msg: Message) -> dict[str, str | None]:
        """The same message to every channel: {channel: None | error string}.

        Failures are reported, not raised: a broken Discord webhook must not
        suppress the Telegram alert about the money.
        """
        async def one(name):
            return name, await self.send_to(name, msg)

        return dict(await asyncio.gather(*(one(n.name) for n in self._notifiers)))
