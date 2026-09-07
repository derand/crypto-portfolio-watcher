"""Runtime objects passed between adapters, the pipeline and notifiers.

Config objects live in config.py and are pydantic; these are plain dataclasses
because they are constructed hot, per address, per tick.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_display(raw: int, decimals: int, max_dp: int = 6) -> str:
    """Same value, trimmed for human eyes.

    0.020992603029613899 ETH is technically true and practically unreadable;
    a digest is for reading, not for auditing wei.
    """
    text = format_units(raw, decimals)
    if "." not in text:
        return text
    whole, frac = text.split(".")
    # Keep enough digits to see a small balance, but never a wall of them.
    keep = max_dp if whole not in ("0", "-0") else max_dp + 2
    frac = frac[:keep].rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def format_units(raw: int, decimals: int) -> str:
    """Integer base units to a human string, trailing zeros trimmed.

    Integer arithmetic only: floats lose satoshis and wei, and a portfolio tool
    that quietly rounds balances is worse than none.
    """
    sign = "-" if raw < 0 else ""
    whole, frac = divmod(abs(raw), 10 ** decimals) if decimals else (abs(raw), 0)
    if not frac:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{frac:0{decimals}d}".rstrip("0").rstrip(".")


class EventKind(str, Enum):
    TRANSFER = "transfer"          # real movement of value; this is what alerts
    ACCRUAL = "accrual"            # yield: rebase, share price, external state. Digest only.
    POSITION_CHANGE = "position_change"
    ANOMALY = "anomaly"            # balance moved but no transfer explains it
    SERVICE = "service"            # provider down, config problem, self-test


class Direction(str, Enum):
    IN = "in"
    OUT = "out"
    INTERNAL = "internal"          # between two of our own addresses


class Severity(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


@dataclass(slots=True)
class Probe:
    """Cheap 'did anything change?' answer. One HTTP call per address."""
    changed: bool
    marker: str                    # opaque per-chain cursor: tx_count, balance, signature
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BalanceSnapshot:
    asset_key: str                 # "ethereum:native" or "ethereum:0xa0b8..."
    amount_raw: int                # integer base units; never float
    decimals: int
    symbol: str = ""
    block_height: int | None = None
    fee_bearing: bool = False
    """True for assets that also pay transaction fees (EVM native coins).

    Their balance drifts down for reasons no transfer describes - an approve, a
    failed swap, any contract call. A negative unexplained residual on such an
    asset is fees, not a missing transfer, and must not cry wolf.
    """
    debt: bool = False
    """True when `amount_raw` is negative because the balance is money owed.

    Not a third kind of yield flag: a debt residual is interest when it is small
    and a borrow when it is large, so the pipeline sizes it rather than trusting
    a flag. See TokenCfg.debt."""
    yield_bearing: bool = False
    """True for shares whose redemption value grows on its own (ERC-4626).

    The mirror image of fee_bearing: here a *positive* unexplained residual is
    accrued yield rather than money appearing from nowhere, and belongs in the
    digest instead of an alert.
    """


@dataclass(slots=True)
class Transfer:
    tx_hash: str
    uid: str                       # stable per-provider id; the dedup key
    asset_key: str
    amount_raw: int
    direction: Direction
    counterparty: str | None
    block_height: int | None
    ts: datetime
    symbol: str = ""
    decimals: int = 18
    balance_effect: int | None = None
    """Signed change this transfer makes to the balance.

    Defaults to +/- amount_raw. EVM native sends differ: the balance also drops
    by the gas fee, which is not part of the amount anyone cares to be told
    about. Reconciliation uses this; the notification shows amount_raw.
    """

    @property
    def effect(self) -> int:
        if self.balance_effect is not None:
            return self.balance_effect
        return self.amount_raw if self.direction is Direction.IN else -self.amount_raw


@dataclass(slots=True)
class Position:
    """A holding that is not a plain balance: a perp, a vault share, a stake.

    amount_raw is a scaled integer like every other amount in this codebase -
    position sizes arrive as decimal strings and must never become floats.
    """
    protocol: str
    key: str                       # "perp:ETH" | "spot:USDC" | "account"
    symbol: str
    amount_raw: int
    decimals: int
    asset_key: str = ""
    contract: str = ""
    """The venue's own id for the asset, when a symbol is not proof of identity.
    Hyperliquid spot tokens are named by whoever deploys them, so "UBTC" is a
    claim, while token index 197 is the thing itself - and it is the index that
    decides which order book prices it."""
    usd: float | None = None
    accrues: bool = False
    """True when this position's amount climbs on its own - a validator earning
    attestation rewards. Such a change is yield: it belongs in the digest, and
    alerting on it would fire every epoch forever."""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Trade:
    """What a venue reports about a position that was closed or cut down.

    Built from the venue's own fills, never from our last snapshot: the snapshot
    is up to one interval old, and a position is usually closed *because* the
    price moved, so the stale number is worst exactly when it matters most. A
    venue that cannot answer produces no Trade at all, and the event then says
    what closed without saying what it made - which is the honest report.
    """
    pnl_usd: float
    """Realised, as the venue computes it. Fees are `fee_usd`, not deducted
    here, and funding paid while the position was open is in neither."""
    exit_px: str = ""
    fee_usd: float | None = None
    liquidated: bool = False
    """The position was closed by the exchange rather than by its owner. Worth
    its own word in the alert: the number alone does not say who decided."""


@dataclass(slots=True)
class Block:
    """One section of a Message, as a table the channel may lay out itself.

    A channel that has a monospace font and a way to hide bulk behind a tap
    uses both; everything else gets `lines` as plain text. The digest is the
    reason this exists: eighteen aligned columns rendered in Telegram's
    proportional font are a wall, and the same lines in a `<pre>` are a table.
    """
    lines: list[str]
    title: str = ""
    collapsed: bool = False
    """The channel may hide these lines behind a tap. Never put anything here
    that must be seen without one."""
    mono: bool = True


def flatten(blocks: list["Block"]) -> str:
    """Blocks as plain text, for channels and terminals with no layout."""
    out: list[str] = []
    for block in blocks:
        if block.title:
            out.append(block.title)
        out.extend(block.lines)
        out.append("")
    return "\n".join(out).rstrip("\n")


@dataclass(slots=True)
class Message:
    """What a Notifier renders. Channels format it their own way."""
    title: str
    body: str
    severity: Severity = Severity.NORMAL
    kind: EventKind = EventKind.SERVICE
    blocks: list[Block] = field(default_factory=list)
    """Structured form of the same content. `body` stays authoritative: a
    channel that ignores blocks must still say everything."""
