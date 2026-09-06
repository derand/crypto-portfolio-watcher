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

import httpx

from ..chains.base import Target
from ..models import Position
from ..retry import with_retry

log = logging.getLogger(__name__)

API = "https://ethereum-beacon-api.publicnode.com"
GWEI = 10 ** 9
NATIVE_DECIMALS = 18
MARKER_DP = 3          # milli-ETH: the balance ticks every epoch, see _marker


class BeaconSource:
    name = "beacon"

    def __init__(self, validators: dict[str, list[int]] | None = None,
                 url: str = API, client: httpx.AsyncClient | None = None):
        self._validators = validators or {}      # lowercased address -> indexes
        self._url = url.rstrip("/")
        self._client = client
        self._own = client is None

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

        positions = []
        for entry in await self._states(indexes):
            index = entry.get("index")
            balance = int(entry.get("balance", 0)) * GWEI
            validator = entry.get("validator") or {}
            positions.append(Position(
                protocol="beacon", key=f"validator:{index}", symbol="ETH",
                amount_raw=balance, decimals=NATIVE_DECIMALS,
                asset_key="ethereum:native",
                # The balance grows every epoch; that is yield, not an event.
                accrues=True,
                extra={"status": entry.get("status", ""),
                       "slashed": str(validator.get("slashed", "")),
                       "effective": str(int(validator.get("effective_balance", 0))
                                        // GWEI)}))
        positions.sort(key=lambda p: p.key)
        return positions, self._marker(positions)

    @staticmethod
    def _marker(positions: list[Position]) -> str:
        """Balance rounded to milli-ETH, plus status.

        At full precision the balance changes every epoch and every tick would
        look busy while telling us nothing new. A thousandth of an ETH moves
        about twice a day at mainnet rates - often enough that the reported
        figure stays honest. Status is exact: an exit or a slashing must never
        be rounded away.
        """
        parts = []
        for p in positions:
            coarse = p.amount_raw // (10 ** (NATIVE_DECIMALS - MARKER_DP))
            parts.append(f"{p.key}={coarse}:{p.extra.get('status', '')}"
                         f":{p.extra.get('slashed', '')}")
        return "|".join(parts)
