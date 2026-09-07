"""Holdings that are not plain balances.

A chain adapter answers "what does this address hold and what moved". A position
source answers "what does this address have open" - a perp with a liquidation
price, a vault share whose price drifts, a stake with claimable rewards. They
are polled per address, selected by the address's `watch` list.
"""

from typing import Protocol

from ..models import Position
from ..chains.base import Target


class PositionSource(Protocol):
    name: str                        # must match the `watch` keyword

    async def fetch(self, t: Target) -> tuple[list[Position], str]:
        """Return the current positions and an opaque marker.

        The marker lets the caller skip diffing when nothing moved; it is
        compared, never parsed.
        """
        ...

    # Optional. A source that can say what a position *made* implements this;
    # the pipeline checks for it and does nothing where it is absent.
    #
    #   async def trades(self, t: Target, since_ms: int | None) -> dict[str, Trade]
    #
    # Keyed by position key, so the answer lands on the event that reports the
    # close. It is called only on a tick where a position shrank or vanished -
    # it costs a request, and asking on every tick would undo the point of the
    # cheap probe. `since_ms` is when this address was last scanned; a source
    # that cannot filter by time may ignore it, but must never report a fill
    # from before it, because the trade it belongs to was already reported.
