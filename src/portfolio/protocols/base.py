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
