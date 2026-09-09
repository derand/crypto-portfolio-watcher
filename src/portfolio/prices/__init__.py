"""Price lookup with a database-backed cache.

One refresh per tick, batched per source, then every consumer reads from memory.
Prices are advisory: an asset with no price still gets watched and still gets
alerted - it just cannot be measured against the USD threshold.
"""

import logging
from datetime import datetime, timedelta, timezone

from .sources import NATIVE_IDS, STABLE_AT_ONE, PriceSources

log = logging.getLogger(__name__)

__all__ = ["PriceBook", "PriceSources", "build_prices"]


def build_prices(cfg, conn):
    return PriceBook(conn, PriceSources(cfg.api_keys.coingecko),
                     ttl_minutes=cfg.prices.ttl_minutes,
                     max_age_minutes=cfg.prices.max_age_minutes)


class PriceBook:
    def __init__(self, conn, sources: PriceSources, ttl_minutes: int = 15,
                 max_age_minutes: int = 180):
        self._conn = conn
        self._sources = sources
        self._ttl = timedelta(minutes=ttl_minutes)
        self._max_age = timedelta(minutes=max_age_minutes)
        self._cache: dict[str, float] = {}
        """Only what is currently young enough to quote.

        Rebuilt from the database on every refresh, never added to across them.
        It used to only ever grow: a price fetched once stayed in memory, so a
        long-lived `watch` that lost its price source went on answering with
        whatever it had last seen - for days - while `refresh` logged a warning
        nobody reads and every total on screen looked current."""

    async def aclose(self) -> None:
        await self._sources.aclose()

    def usd(self, asset_key: str) -> float | None:
        return self._cache.get(asset_key)

    def value(self, asset_key: str, amount_raw: int, decimals: int) -> float | None:
        price = self._cache.get(asset_key)
        if price is None:
            return None
        return amount_raw / (10 ** decimals) * price

    def _reload(self, ttl: timedelta) -> set[str]:
        """Rebuild the cache from stored prices; return the ones still fresh.

        Two ages, because they answer different questions. `max_age` decides
        what may still be quoted at all and so what the cache holds; `ttl`
        decides what is recent enough not to ask about again, and is what the
        caller gets back. Everything older than `max_age` is simply absent,
        which is how an asset becomes unpriced rather than silently stale.

        Ordered oldest first so that the newest row for an asset is the one left
        in the dictionary.
        """
        now = datetime.now(timezone.utc)
        # max(): a TTL longer than max_age would otherwise ask for rows the
        # cache is not allowed to hold, and report them fresh.
        cutoff = (now - max(self._max_age, ttl)).isoformat()
        rows = self._conn.execute(
            """SELECT a.asset_key, p.usd, p.ts FROM prices p
                 JOIN assets a ON a.id = p.asset_id
                WHERE p.ts >= ? ORDER BY p.ts""", (cutoff,)).fetchall()
        quotable = (now - self._max_age).isoformat()
        self._cache = {r["asset_key"]: r["usd"] for r in rows if r["ts"] >= quotable}
        still_fresh = (now - ttl).isoformat()
        return {r["asset_key"] for r in rows if r["ts"] >= still_fresh}

    async def refresh(self, assets: list[dict], ttl_minutes: int | None = None) -> None:
        """assets: dicts with asset_key, chain, contract, symbol, coingecko_id.

        `ttl_minutes` overrides the configured TTL for this one call. The tick
        loop leaves it alone; a command answering a person passes the shorter
        one, because a total somebody is looking at is worth four calls and a
        total nobody asked for is not.
        """
        ttl = self._ttl if ttl_minutes is None else timedelta(minutes=ttl_minutes)
        fresh = self._reload(ttl)
        stale = [a for a in assets if a["asset_key"] not in fresh]
        if not stale:
            return

        found: dict[str, float] = {}
        native_ids: dict[str, list[str]] = {}
        tokens: dict[str, list[str]] = {}
        hyperliquid: list[dict] = []

        for a in stale:
            key, chain, contract = a["asset_key"], a["chain"], a["contract"]
            if chain == "hyperliquid":
                hyperliquid.append(a)
            elif contract and a.get("coingecko_id"):
                # An explicit id wins over the contract lookup: it is how a
                # wrapper or a vault share says "price me as this asset". Vault
                # shares are listed nowhere by their own address.
                native_ids.setdefault(a["coingecko_id"], []).append(key)
            elif contract:
                tokens.setdefault(chain, []).append(contract)
            else:
                coin_id = a.get("coingecko_id") or NATIVE_IDS.get(chain)
                if coin_id:
                    native_ids.setdefault(coin_id, []).append(key)
                else:
                    log.debug("no price route for %s", key)

        if native_ids:
            try:
                prices = await self._sources.coingecko_natives(list(native_ids))
                for coin_id, keys in native_ids.items():
                    if coin_id in prices:
                        found.update({k: prices[coin_id] for k in keys})
            except Exception as e:                       # noqa: BLE001
                log.warning("native prices unavailable: %s", e)

        for chain, contracts in tokens.items():
            try:
                prices = await self._sources.coingecko_tokens(chain, contracts)
            except Exception as e:                       # noqa: BLE001
                log.warning("token prices unavailable on %s: %s", chain, e)
                prices = {}
            for contract in contracts:
                key = f"{chain}:{contract}"
                if contract in prices:
                    found[key] = prices[contract]
                    continue
                try:
                    # Long tail: CoinGecko lists a fraction of what exists.
                    price = await self._sources.dexscreener(chain, contract)
                except Exception as e:                   # noqa: BLE001
                    log.warning("dexscreener failed for %s: %s", key, e)
                    price = None
                if price is not None:
                    found[key] = price
                else:
                    log.info("no price for %s; it stays unpriced, not zero", key)

        if hyperliquid:
            mids = {}
            try:
                mids = await self._sources.hyperliquid_mids()
            except Exception as e:                       # noqa: BLE001
                log.warning("hyperliquid mids unavailable: %s", e)
            spot = {}
            # Only fetch the spot book when something needs it: perps, staked
            # HYPE and USDC are all answered by allMids alone.
            if any(a["symbol"] not in STABLE_AT_ONE and a["symbol"] not in mids
                   for a in hyperliquid):
                try:
                    spot = await self._sources.hyperliquid_spot_mids()
                except Exception as e:                   # noqa: BLE001
                    log.warning("hyperliquid spot mids unavailable: %s", e)
            for a in hyperliquid:
                symbol = a["symbol"]
                # Only a numeric contract is a spot token index. Rows written
                # before indexes were recorded carry the coin name there, and
                # reading that as an index loses the price entirely.
                index = a["contract"] if (a["contract"] or "").isdigit() else ""
                if symbol in STABLE_AT_ONE:
                    found[a["asset_key"]] = 1.0          # the quote currency itself
                elif index and index in spot:
                    # The token's own order book, found by index: a spot token
                    # named UBTC is priced as itself, never as bitcoin.
                    found[a["asset_key"]] = spot[index]
                elif symbol in mids:
                    # Perps and staking have no token index; there the coin name
                    # is Hyperliquid's own and unambiguous.
                    found[a["asset_key"]] = mids[symbol]
                elif symbol in spot:
                    found[a["asset_key"]] = spot[symbol]
                else:
                    log.info("no hyperliquid price for %s; it stays unpriced", symbol)

        self._store(found)
        self._cache.update(found)

    def _store(self, prices: dict[str, float]) -> None:
        if not prices:
            return
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for key, usd in prices.items():
            row = self._conn.execute("SELECT id FROM assets WHERE asset_key=?",
                                     (key,)).fetchone()
            if row:
                rows.append((row["id"], usd, "market", now))
        self._conn.execute("BEGIN")
        try:
            self._conn.executemany(
                "INSERT OR REPLACE INTO prices(asset_id, usd, source, ts) VALUES (?,?,?,?)",
                rows)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
