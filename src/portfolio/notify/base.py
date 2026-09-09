"""The one interface every channel implements.

Adding a channel means adding a file and a line in router.build_notifiers.
"""

from typing import Protocol

from ..models import Message


class Notifier(Protocol):
    name: str

    async def send(self, msg: Message) -> None:
        """Deliver msg, or raise. Retrying is this method's own job.

        It cannot be the caller's: a Message is not always one request. Telegram
        splits a long digest into several, and retrying the whole send after the
        second one failed re-delivers the first. So a notifier that makes more
        than one call retries each of them separately, and one that makes a
        single call wraps it in `retry.with_retry` itself.
        """
        ...
