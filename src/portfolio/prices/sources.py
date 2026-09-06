"""Where a USD price comes from, per kind of asset.

Three sources, in order of trust:
  CoinGecko    - natives and listed tokens, batched
  Hyperliquid  - its own mark prices; authoritative for its own book
  DexScreener  - last resort for tokens CoinGecko has never heard of
"""

import logging

import httpx

from ..retry import with_retry

log = logging.getLogger(__name__)

CG = "https://api.coingecko.com/api/v3"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens"
HL_INFO = "https://api.hyperliquid.xyz/info"

# our chain name -> CoinGecko coin id of its native coin
NATIVE_IDS = {"bitcoin": "bitcoin", "ethereum": "ethereum", "arbitrum": "ethereum",
              "base": "ethereum", "bsc": "binancecoin", "polygon": "matic-network"}

# our chain name -> CoinGecko asset platform id
CG_PLATFORM = {"ethereum": "ethereum", "arbitrum": "arbitrum-one", "base": "base",
               "bsc": "binance-smart-chain", "polygon": "polygon-pos"}

# our chain name -> DexScreener chainId
DS_CHAIN = {"ethereum": "ethereum", "arbitrum": "arbitrum", "base": "base",
            "bsc": "bsc", "polygon": "polygon"}

STABLE_AT_ONE = {"USDC", "USD"}


class PriceSources:
    def __init__(self, api_key: str = "", client: httpx.AsyncClient | None = None,
                 cg_url: str = CG, ds_url: str = DEXSCREENER, hl_url: str = HL_INFO):
        self._key = api_key
        self._client = client
        self._own = client is None
        self._cg, self._ds, self._hl = cg_url, ds_url, hl_url

    async def aclose(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=25.0)
        return self._client

    def _headers(self) -> dict:
        return {"x-cg-demo-api-key": self._key} if self._key else {}

    async def _get_json(self, url: str, params=None, headers=None, what="request"):
        async def call():
            r = await self._http().get(url, params=params, headers=headers or {})
            r.raise_for_status()
            return r.json()
        return await with_retry(call, what=what)

    async def coingecko_natives(self, coin_ids: list[str]) -> dict[str, float]:
        if not coin_ids:
            return {}
        data = await self._get_json(
            f"{self._cg}/simple/price",
            params={"ids": ",".join(sorted(set(coin_ids))), "vs_currencies": "usd"},
            headers=self._headers(), what="coingecko simple/price")
        return {k: float(v["usd"]) for k, v in data.items() if v.get("usd") is not None}

    async def coingecko_tokens(self, chain: str, contracts: list[str]) -> dict[str, float]:
        platform = CG_PLATFORM.get(chain)
        if not platform or not contracts:
            return {}
        data = await self._get_json(
            f"{self._cg}/simple/token_price/{platform}",
            params={"contract_addresses": ",".join(contracts), "vs_currencies": "usd"},
            headers=self._headers(), what=f"coingecko token_price/{platform}")
        return {k.lower(): float(v["usd"]) for k, v in data.items()
                if v.get("usd") is not None}

    async def dexscreener(self, chain: str, contract: str) -> float | None:
        """Deepest pool for this token *on this chain*.

        The chain filter is not optional. Querying the Ethereum USDC address
        returns pairs from every network the address exists on, and the deepest
        of them was an unrelated token on PulseChain priced at $0.0009 - a
        thousandfold error waiting to be believed.
        """
        want_chain = DS_CHAIN.get(chain)
        if not want_chain:
            return None
        data = await self._get_json(f"{self._ds}/{contract}",
                                    what=f"dexscreener {contract[:10]}")
        best, best_liquidity = None, 0.0
        for pair in data.get("pairs") or []:
            if pair.get("chainId") != want_chain:
                continue
            base = (pair.get("baseToken") or {}).get("address", "")
            if base.lower() != contract.lower():
                continue
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
            price = pair.get("priceUsd")
            if price and liquidity > best_liquidity:
                best, best_liquidity = float(price), liquidity
        return best

    async def hyperliquid_spot_mids(self) -> dict[str, float]:
        """Spot mid price per token index, from USDC order books only.

        Three traps, all paid for once:
          - the contexts are not parallel to the pair list; they carry the pair
            name and must be matched by it. Zipping them lines UBTC up with a
            dead market and prices bitcoin at six hundredths of a cent.
          - a pair quoted in anything but USDC is not a dollar price. UBTC/USDH
            exists and is empty.
          - a token's name is chosen by whoever deploys it, so the answer is
            keyed by token index. Names are a label; the index is the asset.
        Where a token has several USDC books, the busiest one wins. The result
        is keyed by index and, for names that only one token carries, by name
        too - so a holding registered before the index was recorded can still
        be priced, and an ambiguous name is priced by nobody.
        """
        async def call():
            r = await self._http().post(self._hl, json={"type": "spotMetaAndAssetCtxs"})
            r.raise_for_status()
            return r.json()
        data = await with_retry(call, what="hyperliquid spotMeta")
        try:
            meta, ctxs = data[0], data[1]
        except (KeyError, IndexError, TypeError):
            log.warning("unexpected spotMetaAndAssetCtxs shape")
            return {}
        names = {t["index"]: t["name"] for t in meta.get("tokens") or []}
        context = {c.get("coin"): c for c in ctxs if isinstance(c, dict)}
        out: dict[str, float] = {}
        best: dict[str, float] = {}
        for pair in meta.get("universe") or []:
            tokens = pair.get("tokens") or []
            if len(tokens) != 2 or names.get(tokens[1]) != "USDC":
                continue
            ctx = context.get(pair.get("name")) or {}
            try:
                mid = float(ctx["midPx"])
                volume = float(ctx.get("dayNtlVlm") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            key = str(tokens[0])
            if mid > 0 and volume >= best.get(key, -1.0):
                out[key], best[key] = mid, volume
        by_name: dict[str, float] = {}
        seen: dict[str, int] = {}
        for index, mid in out.items():
            name = names.get(int(index), "")
            seen[name] = seen.get(name, 0) + 1
            by_name[name] = mid
        out.update({n: m for n, m in by_name.items() if n and seen[n] == 1})
        return out

    async def hyperliquid_mids(self) -> dict[str, float]:
        async def call():
            r = await self._http().post(self._hl, json={"type": "allMids"})
            r.raise_for_status()
            return r.json()
        data = await with_retry(call, what="hyperliquid allMids")
        out = {}
        for coin, price in data.items():
            if coin.startswith("#"):          # spot pair indices, not symbols
                continue
            try:
                out[coin] = float(price)
            except (TypeError, ValueError):
                continue
        return out
