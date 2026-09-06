"""Exponential backoff with jitter. Deliberately not a dependency."""

import asyncio
import logging
import random

log = logging.getLogger(__name__)


def redact(text: str, *secrets: str) -> str:
    """Remove API keys from text that is about to become a message.

    httpx puts the full URL in every HTTPStatusError, and two providers carry
    their key in that URL - Alchemy as a path segment, Etherscan as `apikey=`.
    That text travels: an adapter failure lands in `ScanResult.failed`, and the
    bot prints those straight into a Telegram message. So a provider answering
    429 was enough to publish the key to the chat.
    """
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    return text


class Unavailable(Exception):
    """Provider failed every attempt. Callers treat this as 'no data this tick'."""


class Permanent(Exception):
    """Retrying cannot help: bad credentials, bad request, unknown chat.

    Raised by adapters so backoff does not burn three attempts and 30 seconds
    re-asking a question that already has a final answer.
    """


async def with_retry(fn, *, attempts=3, base=1.0, cap=30.0, what="request"):
    """Call async fn() up to `attempts` times, backing off with full jitter.

    Full jitter (sleep uniformly in [0, delay]) rather than fixed delay: several
    addresses hitting the same provider must not retry in lockstep.
    """
    last = None
    for i in range(attempts):
        try:
            return await fn()
        except Permanent:
            raise
        except Exception as e:  # noqa: BLE001 - provider errors are opaque by nature
            last = e
            if i == attempts - 1:
                break
            delay = min(cap, base * (2 ** i))
            wait = random.uniform(0, delay)
            log.warning("%s failed (%s/%s): %s; retry in %.1fs", what, i + 1, attempts, e, wait)
            await asyncio.sleep(wait)
    raise Unavailable(f"{what} failed after {attempts} attempts: {last}") from last
