"""The portfolio's value as a series, rebuilt from what each tick wrote down.

Two carried-forward series multiplied: quantities from `balance_snapshots` and
`position_snapshots`, prices from `prices`, each held at its last known value
until the next row. Carrying forward is the correct reading of how both are
sampled, not a patch over missing data - a row is written when something is
fetched, and nothing in between is unknown, only unchanged.

`balance_snapshots.usd` is deliberately not used: it is written when the
balance changes, never when the price does, so a series read off it draws
bitcoin as a flat line through every move in the market.

The total at each point is `digest.total_of` - the valuation the digest and
/portfolio use, asked about a moment in the past - and the set of what counts
is today's: enabled addresses, whitelisted assets. An asset added to the
whitelist mid-period appears as a step on the day it was first fetched, since
no quantity was recorded before that and none can be invented.
"""

import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .digest import _scale, total_of
from .protocols.hyperliquid import COLLATERAL

HISTORY_SINCE = datetime(2026, 9, 10, tzinfo=timezone.utc)
"""Where the series starts: the schema v6 migration, which created
`position_snapshots`. It seeded each position held then with one row stamped
with when that position last *changed*, so before this moment a validator held
all along appears on the day it last moved, and the total is short by it - 43%
on the first digest it was compared against. Quantities from before cannot be
recovered, so the series does not pretend to have them."""

NET_SINCE = datetime(2026, 10, 27, 22, 37, tzinfo=timezone.utc)
"""When the Hyperliquid source started reporting the collateral net of the
perps' unrealised PnL (PLAN §6). Before it, `spot:USDC` is the venue's `total`,
which includes that PnL; a net row with a perp open carries `extra.total`, so
the first such row is the real switch-over and this is only the fallback for a
database that never held a perp after it - no row older than this can be net."""

SAME_TICK = timedelta(seconds=60)
"""How far after a collateral row the perps of the same tick may be written.
One address's rows land within milliseconds; the next tick is minutes away. A
perp closed on that tick writes its terminal zero after the collateral row, and
the venue has already realised its PnL into the collateral - read at the row's
own timestamp, the perp is still open and its PnL is taken out twice."""


def _ts(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class _Steps:
    """A value that holds from one timestamp until the next."""

    def __init__(self):
        self.ts: list[datetime] = []
        self.values: list = []

    def add(self, ts: datetime, value) -> None:
        self.ts.append(ts)
        self.values.append(value)

    def at(self, t: datetime):
        """(timestamp, value) of the last row at or before t, or None."""
        i = bisect_right(self.ts, t)
        return (self.ts[i - 1], self.values[i - 1]) if i else None


class _PricesAt:
    """The PriceBook interface, answering as of one moment.

    A price older than `max_age` at that moment is absent, as it is in the
    live book: the asset becomes unpriced rather than quoted at a stale rate.
    """

    def __init__(self, series: dict[str, _Steps], t: datetime, max_age: timedelta):
        self._series, self._t, self._max_age = series, t, max_age

    def usd(self, asset_key: str) -> float | None:
        steps = self._series.get(asset_key)
        found = steps.at(self._t) if steps else None
        if found is None or self._t - found[0] > self._max_age:
            return None
        return found[1]

    def value(self, asset_key: str, amount_raw: int, decimals: int) -> float | None:
        price = self.usd(asset_key)
        return None if price is None else amount_raw / (10 ** decimals) * price


@dataclass(frozen=True)
class Point:
    ts: datetime
    usd: float
    unpriced: int


class History:
    """Everything the series is built from, loaded once."""

    def __init__(self, conn, max_age: timedelta, since: datetime = HISTORY_SINCE):
        self._max_age = max_age
        self._balances: dict[tuple, _Steps] = {}
        self._balance_meta: dict[tuple, dict] = {}
        for r in conn.execute("""
                SELECT b.address_id, b.asset_id, s.asset_key, s.decimals,
                       b.amount_raw, b.ts
                FROM balance_snapshots b
                JOIN addresses a ON a.id = b.address_id
                JOIN assets s ON s.id = b.asset_id
                WHERE a.enabled = 1 AND s.whitelisted = 1
                ORDER BY b.ts"""):
            key = (r["address_id"], r["asset_id"])
            if key not in self._balances:
                self._balances[key] = _Steps()
                self._balance_meta[key] = {"asset_key": r["asset_key"],
                                           "decimals": r["decimals"]}
            self._balances[key].add(_ts(r["ts"]), int(r["amount_raw"]))

        self._positions: dict[tuple, _Steps] = {}
        self._position_meta: dict[tuple, dict] = {}
        for r in conn.execute("""
                SELECT p.address_id, p.protocol, p.position_key, p.amount_raw,
                       p.extra, p.ts, s.asset_key, s.decimals
                FROM position_snapshots p
                JOIN addresses a ON a.id = p.address_id
                LEFT JOIN assets s ON s.id = p.asset_id
                WHERE a.enabled = 1
                ORDER BY p.ts"""):
            key = (r["address_id"], r["protocol"], r["position_key"])
            if key not in self._positions:
                self._positions[key] = _Steps()
                self._position_meta[key] = {"asset_key": r["asset_key"],
                                            "decimals": r["decimals"]}
            self._positions[key].add(_ts(r["ts"]),
                                     (int(r["amount_raw"]), json.loads(r["extra"] or "{}")))
        self._net_collateral()

        self._prices: dict[str, _Steps] = {}
        for r in conn.execute("""
                SELECT a.asset_key, p.usd, p.ts FROM prices p
                JOIN assets a ON a.id = p.asset_id ORDER BY p.ts"""):
            self._prices.setdefault(r["asset_key"], _Steps()).add(_ts(r["ts"]), r["usd"])

        stamps = [s.ts[-1] for s in self._balances.values()]
        stamps += [s.ts[-1] for s in self._positions.values()]
        firsts = [s.ts[0] for s in self._balances.values()]
        firsts += [s.ts[0] for s in self._positions.values()]
        self.first = max(min(firsts), since) if firsts else None
        self.last = max(stamps) if stamps else None

    def _net_collateral(self) -> None:
        """Take the perps' PnL out of collateral rows recorded before it was.

        Uses the PnL the venue reported at the time, not one recomputed from
        prices: what was folded into the row is the venue's own figure.
        """
        for key, steps in self._positions.items():
            address_id, protocol, position_key = key
            if protocol != "hyperliquid" or position_key != f"spot:{COLLATERAL}":
                continue
            perps = [s for (a, p, k), s in self._positions.items()
                     if a == address_id and p == protocol and k.startswith("perp:")]
            if not perps:
                continue
            net = [ts for ts, (_, extra) in zip(steps.ts, steps.values) if "total" in extra]
            since = net[0] if net else NET_SINCE
            scale = _scale(self._position_meta[key])
            for i, (ts, (amount, extra)) in enumerate(zip(steps.ts, steps.values)):
                if ts >= since:
                    break
                pnl = Decimal(0)
                for perp in perps:
                    found = perp.at(ts + SAME_TICK)
                    if found is None or found[1][0] == 0:
                        continue
                    pnl += _decimal(found[1][1].get("unrealized_pnl"))
                if pnl:
                    steps.values[i] = (amount - int(pnl.scaleb(scale)), extra)

    def total_at(self, t: datetime) -> tuple[float, int]:
        prices = _PricesAt(self._prices, t, self._max_age)
        balances = []
        for key, steps in self._balances.items():
            found = steps.at(t)
            if found is None or found[1] == 0:
                continue
            balances.append({**self._balance_meta[key], "amount_raw": found[1]})
        positions = []
        for key, steps in self._positions.items():
            found = steps.at(t)
            # A terminal zero is how a closed position ends; before its first
            # row it was not held yet.
            if found is None or found[1][0] == 0:
                continue
            _, protocol, position_key = key
            amount, extra = found[1]
            row = {**self._position_meta[key], "protocol": protocol,
                   "position_key": position_key, "amount_raw": amount,
                   "usd": None, "extra": extra}
            if position_key.startswith("perp:"):
                row["extra"] = self._perp_extra(row, prices)
            positions.append(row)
        return total_of(balances, positions, prices)

    @staticmethod
    def _perp_extra(row: dict, prices: _PricesAt) -> dict:
        """The perp's unrealised PnL as of this moment: size × (price − entry).

        The recorded figure is written only on ticks where something else moved,
        so it is sparse; the entry price changes only with the size, so every
        row carries the one this moment needs. The recorded PnL stays where no
        price was quoted.
        """
        extra = row["extra"]
        asset_key = (row["asset_key"]
                     or f"{row['protocol']}:{row['position_key'].split(':')[-1]}")
        price = prices.usd(asset_key)
        try:
            entry = float(extra["entry_px"])
        except (KeyError, TypeError, ValueError):
            return extra
        if price is None:
            return extra
        size = row["amount_raw"] / 10 ** _scale(row)
        return {**extra, "unrealized_pnl": size * (price - entry)}

    def series(self, since: datetime | None = None, until: datetime | None = None,
               step: timedelta = timedelta(hours=1)) -> list[Point]:
        """One point per step on the hour, and one at the last scan.

        The last point is the database's latest state, so a chart ends where
        /portfolio would start, not up to an hour before it.
        """
        if self.first is None:
            return []
        start = max(since, self.first) if since else self.first
        end = min(until, self.last) if until else self.last
        if start > end:
            return []
        t = start.replace(minute=0, second=0, microsecond=0)
        if t < start:
            t += step
        stamps = []
        while t <= end:
            stamps.append(t)
            t += step
        if not stamps or stamps[-1] < end:
            stamps.append(end)
        return [Point(t, *self.total_at(t)) for t in stamps]


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except InvalidOperation:
        return Decimal(0)
