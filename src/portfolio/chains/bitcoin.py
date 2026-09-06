"""Bitcoin via mempool.space. No API key, no account.

One request to /address/{addr} returns both the balance and the transaction
counters, so probe() costs exactly one call and already carries the balance -
the transaction list is only fetched when those counters actually moved.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from ..models import BalanceSnapshot, Direction, Probe, Transfer, format_units
from ..retry import with_retry
from .base import AddressState, Cursor, Target

log = logging.getLogger(__name__)

ASSET_KEY = "bitcoin:native"
DECIMALS = 8
PAGE = 25          # mempool.space returns 25 per page after the first 50
MAX_PAGES = 10     # 250 transactions is far more than one interval can produce


def _sats(stats: dict) -> int:
    return stats["funded_txo_sum"] - stats["spent_txo_sum"]


def format_btc(sats: int) -> str:
    return format_units(sats, DECIMALS)


class BitcoinAdapter:
    chain = "bitcoin"

    def __init__(self, base_url: str = "https://mempool.space/api",
                 client: httpx.AsyncClient | None = None,
                 min_interval: float = 0.3):
        self._base = base_url.rstrip("/")
        self._client = client
        self._own = client is None
        self._min_interval = min_interval
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def _get(self, path: str):
        """Serialised and paced: mempool.space is free and unmetered, so we are
        the ones responsible for not hammering it."""
        async with self._lock:
            gap = time.monotonic() - self._last_call
            if gap < self._min_interval:
                await asyncio.sleep(self._min_interval - gap)
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=20.0)
            try:
                async def call():
                    r = await self._client.get(f"{self._base}{path}")
                    r.raise_for_status()
                    return r.json()
                return await with_retry(call, what=f"mempool.space {path}")
            finally:
                self._last_call = time.monotonic()

    async def aclose(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    def scopes(self, t: Target) -> list[str]:
        return [""]                     # one address, one place

    async def probe(self, t: Target, scope: str, cursor: Cursor) -> Probe:
        data = await self._get(f"/address/{t.address}")
        chain_stats, mem = data["chain_stats"], data["mempool_stats"]
        # The marker folds in the mempool too: a pending transaction changes the
        # marker on arrival and again when it confirms, so both are noticed.
        marker = (f"{chain_stats['tx_count']}:{_sats(chain_stats)}:"
                  f"{mem['tx_count']}:{_sats(mem)}")
        return Probe(changed=marker != cursor.last_marker, marker=marker, raw=data)

    async def fetch(self, t: Target, scope: str, cursor: Cursor,
                    probe: Probe) -> AddressState:
        address = t.address
        chain_stats = probe.raw["chain_stats"]
        mem = probe.raw["mempool_stats"]
        confirmed = _sats(chain_stats)

        balances = [BalanceSnapshot(asset_key=ASSET_KEY,
                                    amount_raw=confirmed + _sats(mem),
                                    decimals=DECIMALS, symbol="BTC")]
        state = AddressState(balances=balances,
                             cursor=Cursor(last_marker=probe.marker,
                                           last_item=cursor.last_item,
                                           last_block=cursor.last_block))

        if cursor.is_fresh:
            # First sight: adopt the current balance as the baseline. Replaying
            # years of history as notifications would be useless noise.
            newest = await self._newest_txid(address)
            state.cursor = Cursor(last_marker=probe.marker, last_item=newest)
            return state

        txs, truncated = await self._txs_since(address, cursor.last_item)
        state.truncated = truncated
        for tx in txs:
            t = self._to_transfer(tx, address)
            if t is not None:
                state.transfers.append(t)
        # Park on the newest *confirmed* transaction, never on txs[0]. The walk
        # stops at last_item, so parking on an unconfirmed one means that when
        # it confirms it is the stopping point and is never read again: the
        # pending → confirmed requeue never fires and the event says "(in
        # mempool)" forever. Leaving the cursor behind costs one re-read of
        # transfers the database already deduplicates by uid.
        newest = next((tx for tx in txs if (tx.get("status") or {}).get("confirmed")),
                      None)
        if newest:
            state.cursor.last_item = newest["txid"]
            state.cursor.last_block = newest["status"].get("block_height")
        return state

    async def _newest_txid(self, address: str) -> str | None:
        txs = await self._get(f"/address/{address}/txs")
        return txs[0]["txid"] if txs else None

    async def _txs_since(self, address: str, last_item: str | None):
        """Newest-first transactions up to (not including) last_item.

        mempool.space lists unconfirmed transactions first, then confirmed ones,
        so a plain walk covers both.
        """
        out: list[dict] = []
        seen: set[str] = set()
        path = f"/address/{address}/txs"
        for page in range(MAX_PAGES):
            batch = await self._get(path)
            if not batch:
                return out, False
            for tx in batch:
                if tx["txid"] == last_item:
                    return out, False
                if tx["txid"] in seen:      # defensive: mempool/chain overlap
                    continue
                seen.add(tx["txid"])
                out.append(tx)
            last_confirmed = next(
                (t["txid"] for t in reversed(batch) if t["status"].get("confirmed")), None)
            if last_confirmed is None or len(batch) < PAGE:
                return out, False
            path = f"/address/{address}/txs/chain/{last_confirmed}"
        log.warning("%s: more than %d pages of new history; stopping",
                    address, MAX_PAGES)
        return out, True

    @staticmethod
    def _to_transfer(tx: dict, address: str) -> Transfer | None:
        """Net effect of one transaction on one address.

        Net, not gross: an ordinary spend puts change back to the same address,
        so counting outputs alone would report a huge phantom incoming amount.
        """
        credited = sum(v["value"] for v in tx["vout"]
                       if v.get("scriptpubkey_address") == address)
        debited = sum(i["prevout"]["value"] for i in tx["vin"]
                      if i.get("prevout", {}).get("scriptpubkey_address") == address)
        net = credited - debited
        if net == 0:
            return None

        if net > 0:
            others = [i.get("prevout", {}).get("scriptpubkey_address") for i in tx["vin"]]
        else:
            others = [v.get("scriptpubkey_address") for v in tx["vout"]]
        counterparty = next((a for a in others if a and a != address), None)

        status = tx["status"]
        ts = (datetime.fromtimestamp(status["block_time"], tz=timezone.utc)
              if status.get("confirmed") and status.get("block_time")
              else datetime.now(timezone.utc))
        return Transfer(
            tx_hash=tx["txid"],
            uid=tx["txid"],                   # one net event per tx per address
            asset_key=ASSET_KEY,
            symbol="BTC",
            decimals=DECIMALS,
            amount_raw=abs(net),
            direction=Direction.IN if net > 0 else Direction.OUT,
            counterparty=counterparty,
            block_height=status.get("block_height"),
            ts=ts,
        )
