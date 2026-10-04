"""Validator balances from a public beacon node.

The principal is the largest thing a staker owns and the only one no execution
API can see: 32 ETH per validator lives on the consensus layer, invisible to
balances, transfers and token lists alike.

Not beaconcha.in, which the plan originally assumed: it now refuses anonymous
reads outright (401), and its free key is a 30-day trial capped at 1000 requests
- about ten days at one call a tick, then silence. The standard Beacon REST API
that every consensus client exposes gives the same numbers with no key at all,
and several providers publish one.

Rewards, unlike the principal, are not read here. They arrive on the execution
address as EIP-4895 withdrawals, and chains/withdrawals.py reports those as the
transfers they are.
"""

import logging
import time

import httpx

from ..chains.base import Target
from ..models import Position
from ..retry import Unavailable, with_retry

log = logging.getLogger(__name__)

API = "https://ethereum-beacon-api.publicnode.com"
GWEI = 10 ** 9
NATIVE_DECIMALS = 18
MARKER_DP = 3          # milli-ETH: the balance ticks every epoch, see _marker
TREND_WINDOW = 3600.0  # seconds; see _trend
SWEEP_MIN_GWEI = 10 ** 6   # 0.001 ETH: more than an offline validator loses between ticks


class BeaconSource:
    name = "beacon"

    def __init__(self, validators: dict[str, list[int]] | None = None,
                 url: str = API, client: httpx.AsyncClient | None = None,
                 clock=time.monotonic):
        self._validators = validators or {}      # lowercased address -> indexes
        self._url = url.rstrip("/")
        self._client = client
        self._own = client is None
        self._clock = clock
        # index -> [(when, balance in gwei)], oldest first. In memory only: a
        # restart forgets it, and the trend is simply absent for one window.
        self._seen: dict[str, list[tuple[float, int]]] = {}

    async def aclose(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _states(self, indexes: list[int]) -> list[dict]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        ids = ",".join(str(i) for i in indexes)

        async def call():
            r = await self._client.get(
                f"{self._url}/eth/v1/beacon/states/head/validators",
                params={"id": ids}, headers={"accept": "application/json"})
            r.raise_for_status()
            return r.json()

        body = await with_retry(call, what="beacon validators")
        return body.get("data") or []

    async def fetch(self, t: Target) -> tuple[list[Position], str]:
        indexes = self._validators.get(t.address.lower(), [])
        if not indexes:
            return [], ""

        states = await self._states(indexes)
        # Every index asked for has to come back. A validator the answer
        # leaves out is not one that exited - an exit is a status, and the row
        # stays - it is an answer that is short, and read as data it would
        # close the position: a terminal zero in the history and a "closed
        # validator" alert over 32 ETH that never moved.
        got = {str(e.get("index")) for e in states}
        missing = [i for i in indexes if str(i) not in got]
        if missing:
            raise Unavailable(f"{t.label}/beacon: no answer for validator(s) "
                              f"{', '.join(str(i) for i in missing)}")

        positions = []
        for entry in states:
            index = entry.get("index")
            gwei = int(entry.get("balance", 0))
            balance = gwei * GWEI
            validator = entry.get("validator") or {}
            status = entry.get("status", "")
            effective = int(validator.get("effective_balance", 0))
            extra = {"status": status,
                     "slashed": str(validator.get("slashed", "")),
                     "effective": str(effective // GWEI)}
            trend = self._trend(str(index), gwei, effective, status)
            if trend:
                extra["trend"] = trend
            positions.append(Position(
                protocol="beacon", key=f"validator:{index}", symbol="ETH",
                amount_raw=balance, decimals=NATIVE_DECIMALS,
                asset_key="ethereum:native",
                # The balance grows every epoch; that is yield, not an event.
                accrues=True, extra=extra))
        positions.sort(key=lambda p: p.key)
        return positions, self._marker(positions)

    def _trend(self, index: str, gwei: int, effective: int,
               status: str) -> str | None:
        """"up" or "down" against the balance at least an hour ago, or None.

        An online validator gains about 0.00008 ETH an hour. To end the hour
        lower it has to miss most of the ~9 attestations in it, which a working
        one does not do - so a lower balance means it is offline, and nothing
        finer is needed. None until a full window has been seen.

        The withdrawal sweep takes the excess over the effective balance every
        week or so, and that drop is not a loss: a fall of more than a tick's
        worth that still leaves the balance at or above the effective one
        restarts the window instead of being read as one. A slashing lands
        below the effective balance and is read as the loss it is.
        """
        if not status.startswith("active"):
            self._seen.pop(index, None)
            return None
        now = self._clock()
        seen = self._seen.setdefault(index, [])
        if seen and seen[-1][1] - gwei >= SWEEP_MIN_GWEI and gwei >= effective:
            seen.clear()
        seen.append((now, gwei))
        old = [i for i, (when, _) in enumerate(seen) if now - when >= TREND_WINDOW]
        if not old:
            return None
        del seen[:old[-1]]                  # keep the newest reading old enough
        return "up" if gwei > seen[0][1] else "down"

    @staticmethod
    def _marker(positions: list[Position]) -> str:
        """Balance rounded to milli-ETH, plus status.

        At full precision the balance changes every epoch and every tick would
        look busy while telling us nothing new. A thousandth of an ETH moves
        about twice a day at mainnet rates - often enough that the reported
        figure stays honest. Status is exact: an exit or a slashing must never
        be rounded away. So is the trend: an offline validator loses a
        milli-ETH only every half a day, and the marker is what lets the
        pipeline hear about it within the hour.
        """
        parts = []
        for p in positions:
            coarse = p.amount_raw // (10 ** (NATIVE_DECIMALS - MARKER_DP))
            parts.append(f"{p.key}={coarse}:{p.extra.get('status', '')}"
                         f":{p.extra.get('slashed', '')}"
                         f":{p.extra.get('trend', '')}")
        return "|".join(parts)
