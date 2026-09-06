"""The one interface every channel implements.

Adding a channel means adding a file and a line in router.build_notifiers.
"""

from typing import Protocol

from ..models import Message


class Notifier(Protocol):
    name: str

    async def send(self, msg: Message) -> None:
        """Deliver msg, or raise. Retry policy belongs to the caller."""
        ...
