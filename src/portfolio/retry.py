"""Exponential backoff with jitter. Deliberately not a dependency."""

import asyncio
import logging
import random

log = logging.getLogger(__name__)


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
