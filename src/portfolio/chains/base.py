"""The interface every chain implements.

Two calls, deliberately split:
  probe()  - one cheap request answering "did anything change?"
  fetch()  - the expensive part, only run when probe says yes.

`scope` is the second axis: one Bitcoin address lives in one place, but one EVM
address lives on Ethereum, Arbitrum and Base at once, each with its own cursor.
Bitcoin uses the empty scope; EVM uses the network name.
"""

from dataclasses import dataclass, field
from typing import Protocol

from ..models import BalanceSnapshot, Probe, Transfer


@dataclass(slots=True, frozen=True)
class Target:
    """One watched address, as the adapter sees it."""
    address: str
    label: str
    watch: frozenset[str]
    chains: tuple[str, ...] = ()


@dataclass(slots=True)
class Cursor:
    """Where we stopped last time. Persisted per (chain, address, scope)."""
    last_block: int | None = None
    last_item: str | None = None      # newest processed uid
    last_marker: str | None = None    # opaque probe marker

    @property
    def is_fresh(self) -> bool:
        """True on the very first sight of an address in this scope.

        Matters: a fresh address must NOT replay its entire history as alerts.
        We record the balance as a baseline and start watching from now.
        """
        return self.last_item is None and self.last_marker is None


@dataclass(slots=True)
class AddressState:
    balances: list[BalanceSnapshot] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)
    cursor: Cursor = field(default_factory=Cursor)
    truncated: bool = False           # more history than we were willing to walk


class ChainAdapter(Protocol):
    chain: str

    def scopes(self, t: Target) -> list[str]:
        """Which sub-networks to poll for this target."""
        ...

    async def probe(self, t: Target, scope: str, cursor: Cursor) -> Probe: ...

    async def fetch(self, t: Target, scope: str, cursor: Cursor,
                    probe: Probe) -> AddressState: ...
