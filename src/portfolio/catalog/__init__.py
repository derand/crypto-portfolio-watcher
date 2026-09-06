"""Protocol entry points, as data.

The whitelist answers "what is this token worth"; it cannot answer "what am I
holding that I forgot about". `discover-tokens` fills that gap for anything the
provider's token index knows, and PLAN §6 records the two things that index is
blind to by construction: a balance held inside somebody else's contract, and a
token that was never handed to you with a standard `Transfer` event.

This catalog closes both, for the protocols where one contract can be asked to
name all the others. An Aave market names every one of its receipt tokens in a
single call, and does so for the listings it adds next year too - so the
catalog stores the market, never the tokens. Thirteen rows cover ten protocols
across five networks and stay correct without maintenance.

Rules that keep it useful:

  * **Every address here was verified on chain before it was written down.** A
    wrong entry does not fail loudly; it reports a balance of zero, which reads
    exactly like not holding the position. Verification is cheap - an entry that
    answers its own enumeration call is an entry that works - and `pw
    catalog-check` re-runs it.
  * It holds no opinion about what anyone owns. Naming a protocol here means
    "this reader knows how to ask it", not "this is held", which is the
    property that lets the file be public at all (PLAN §14).
  * Nothing here is polled by the tick. Discovery is a manual command; what it
    proposes ends up in `tokens:`, and only that is watched.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

KINDS = {"aave_v3", "compound_v2", "univ3", "slipstream"}
"""How to ask a protocol to enumerate its receipt tokens.

`aave_v3`  - PoolAddressesProvider -> getPoolDataProvider() -> getAllATokens(),
             which answers (symbol, address) pairs. Every Aave v3 fork keeps
             this interface, so one reader serves Aave's own markets, its Lido
             and EtherFi instances, Spark, Seamless and Zerolend alike.
`compound_v2` - Comptroller.getAllMarkets() -> cToken addresses, symbols asked
             of each token separately. The value of a cToken lives in
             exchangeRateStored() rather than in the balance, so what discovery
             proposes for these carries a rate_call.
`univ3`    - a NonfungiblePositionManager. Not a receipt token at all: a
             concentrated-liquidity position is an ERC-721, and what it holds
             has to be computed from the pool price. Read by protocols/univ3.py
             rather than proposed for the whitelist.
`slipstream` - the same interface with one difference that matters: the fourth
             field of positions() is a tick spacing rather than a fee tier, and
             the factory's getPool takes int24 instead of uint24. Calling the
             uint24 form on it reverts, which is how the difference was found.
"""

_FILE = Path(__file__).with_name("positions.yaml")


@dataclass(frozen=True, slots=True)
class Entry:
    protocol: str          # DefiLlama slug, so the name is checkable
    chain: str
    kind: str
    address: str           # the entry point; what it means depends on kind
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.protocol}/{self.chain}"


def load(path: Path | str | None = None) -> list[Entry]:
    """Read the catalog. Malformed rows raise: this file ships with the code,
    so a mistake in it is a bug to fix, not a condition to survive."""
    p = Path(path) if path else _FILE
    raw = yaml.safe_load(p.read_text()) or []
    out = []
    for i, row in enumerate(raw):
        missing = {"protocol", "chain", "kind", "address"} - set(row)
        if missing:
            raise ValueError(f"{p}: entry {i} is missing {sorted(missing)}")
        if row["kind"] not in KINDS:
            raise ValueError(f"{p}: entry {i} has unknown kind {row['kind']!r}; "
                             f"allowed: {sorted(KINDS)}")
        out.append(Entry(protocol=row["protocol"], chain=row["chain"],
                         kind=row["kind"], address=row["address"].lower(),
                         note=row.get("note", "")))
    return out


def by_chain(entries: list[Entry] | None = None) -> dict[str, list[Entry]]:
    out: dict[str, list[Entry]] = {}
    for e in entries if entries is not None else load():
        out.setdefault(e.chain, []).append(e)
    return out
