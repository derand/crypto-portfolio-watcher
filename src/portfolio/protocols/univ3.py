"""Concentrated liquidity positions, from any Uniswap-v3-shaped exchange.

A v3 position is an ERC-721, and the NFT is the position: because each provider
picks a price range, two stakes in the same pool are not interchangeable and
cannot share a fungible LP token the way a v2 pair does. What the NFT stores is
a liquidity coefficient and two tick bounds - never an amount. How much of each
token it holds depends on where the pool's price sits inside those bounds, so
every reading is a computation (protocols/ticks.py).

Three things about this shape drive the design here:

  * **The composition drifts with the price, with no transfer behind it.** Left
    unhandled that is an anomaly every tick, forever, so both legs are marked
    `accrues`. The marker leaves the price out for the same reason - see MARKER.
  * **Uncollected fees are not in the NFT.** `tokensOwed0/1` only move when the
    position is poked, so on a position untouched for years they read zero while
    real fees sit there. The honest reading is to simulate `collect()` with
    `eth_call` and take what it says it would pay - which is why this source
    needs a `from` address on its calls.
  * **The alert worth having is "out of range".** Everything else about a
    position moves continuously; leaving the range is discrete, actionable, and
    means the liquidity has stopped earning and turned into one asset.

Forks differ in one place that matters. Uniswap, PancakeSwap and SushiSwap put a
fee tier in the fourth field of positions() and take uint24 in getPool; Aerodrome
Slipstream puts a tick spacing there and takes int24. Calling the wrong one
reverts, which is how it was found - so the catalog carries the difference as a
separate kind rather than this module guessing.
"""

import logging

from ..chains.abi import decode_address, decode_uint, decode_string, selector
from ..chains.base import Target
from ..chains.evm import EvmAdapter
from ..models import Position
from . import ticks as tickmath

log = logging.getLogger(__name__)

MARKER_TICK_BUCKET = 60
"""Price movement, in ticks, that the marker is allowed to ignore.

The marker answers "is it worth diffing this again". Built from the computed
amounts it would change on every block, because the composition of a range
position moves with the price - the cheap check would then be no check at all,
which is the failure CLAUDE.md warns about. Built from liquidity and bounds
alone it would never change, and the recorded amounts would drift ever further
from the truth between one deposit and the next. So the current tick goes in,
truncated: sixty ticks is about six tenths of a percent of price, small enough
that the stored composition stays honest and large enough that a quiet market
stays quiet. Whether the position is in range is exact - that is the one
transition worth hearing about, and rounding it away would lose it.
"""

MAX_UINT128 = (1 << 128) - 1
WORD = 32


def _signed(value: int, bits: int = 24) -> int:
    """positions() reports ticks as int24 inside a 32-byte word."""
    if value >= 1 << 255:
        return value - (1 << 256)
    return value


def _word(data, index: int) -> int | None:
    if not isinstance(data, str) or not data.startswith("0x"):
        return None
    body = bytes.fromhex(data[2:])
    if len(body) < WORD * (index + 1):
        return None
    return int.from_bytes(body[index * WORD:(index + 1) * WORD], "big")


class UniV3Source:
    name = "univ3"

    def __init__(self, api_key: str = "", entries=None, adapter: EvmAdapter | None = None):
        self._entries = entries or []
        self._adapter = adapter or EvmAdapter(api_key)
        self._own = adapter is None
        # Pool addresses and token metadata never change; a watcher runs for
        # weeks, so asking again every fifteen minutes is pure waste.
        self._pools: dict[tuple, str] = {}
        self._meta: dict[tuple[str, str], tuple[str, int]] = {}

    async def aclose(self) -> None:
        if self._own:
            await self._adapter.aclose()

    def _on(self, chain: str):
        return [e for e in self._entries
                if e.chain == chain and e.kind in ("univ3", "slipstream")]

    async def fetch(self, t: Target) -> tuple[list[Position], str]:
        positions: list[Position] = []
        marks: list[str] = []
        for chain in self._adapter.scopes(t) or list(t.chains):
            for entry in self._on(chain):
                got, mark = await self._market(t, chain, entry)
                positions += got
                marks += mark
        positions.sort(key=lambda p: p.key)
        return positions, "|".join(sorted(marks))

    async def _market(self, t: Target, chain: str, entry):
        nfpm = entry.address
        call = self._adapter.eth_call_many
        (count,) = await call(chain, [(nfpm, selector("balanceOf(address)")
                                       + f"{int(t.address, 16):064x}")])
        held = decode_uint(count) or 0
        if not held:
            return [], []

        ids = [decode_uint(a) for a in await call(chain, [
            (nfpm, selector("tokenOfOwnerByIndex(address,uint256)")
             + f"{int(t.address, 16):064x}{i:064x}") for i in range(held)])]
        ids = [i for i in ids if i is not None]

        raw = await call(chain, [(nfpm, selector("positions(uint256)") + f"{i:064x}")
                                 for i in ids])
        # Fees are what collect() would pay out, not what tokensOwed remembers.
        fees = await call(chain, [
            (nfpm, selector("collect((uint256,address,uint128,uint128))")
             + f"{i:064x}{int(t.address, 16):064x}"
             f"{MAX_UINT128:064x}{MAX_UINT128:064x}") for i in ids],
            sender=t.address)

        parsed = []
        for token_id, answer in zip(ids, raw):
            liquidity = _word(answer, 7)
            if not liquidity:
                continue                     # closed, but the NFT is still owned
            parsed.append({
                "id": token_id,
                "token0": f"0x{_word(answer, 2):040x}",
                "token1": f"0x{_word(answer, 3):040x}",
                "key4": _word(answer, 4),    # a fee tier, or a tick spacing
                "lower": _signed(_word(answer, 5)),
                "upper": _signed(_word(answer, 6)),
                "liquidity": liquidity,
            })
        if not parsed:
            return [], []

        await self._resolve_pools(chain, entry, parsed)
        slots = await self._slots(chain, parsed)
        await self._resolve_meta(chain, parsed)

        out, marks = [], []
        by_id = dict(zip(ids, fees))
        for p in parsed:
            slot = slots.get(p["pool"])
            if slot is None:
                log.warning("%s/%s: no pool state for position %s",
                            t.label, entry.label, p["id"])
                continue
            sqrt_price, tick = slot
            amount0, amount1 = tickmath.amounts_for_liquidity(
                sqrt_price, p["lower"], p["upper"], p["liquidity"])
            inside = tickmath.in_range(tick, p["lower"], p["upper"])
            fee0 = _word(by_id[p["id"]], 0) or 0
            fee1 = _word(by_id[p["id"]], 1) or 0

            for leg, (contract, amount, fee) in enumerate(
                    ((p["token0"], amount0, fee0), (p["token1"], amount1, fee1))):
                symbol, decimals = self._meta.get((chain, contract), ("", 18))
                out.append(Position(
                    protocol=self.name,
                    key=f"{chain}:{p['id']}:{leg}",
                    symbol=symbol,
                    amount_raw=amount,
                    decimals=decimals,
                    asset_key=f"{chain}:{contract}",
                    # The split between the two tokens moves with every trade in
                    # the pool. That is not income and not a transfer; alerting
                    # on it would fire for as long as the position is open.
                    accrues=True,
                    extra={"venue": entry.protocol, "token_id": str(p["id"]),
                           "pool": p["pool"], "tick": str(tick),
                           "tick_lower": str(p["lower"]),
                           "tick_upper": str(p["upper"]),
                           "in_range": "true" if inside else "false",
                           "fees_raw": str(fee)}))
            marks.append(f"{chain}:{p['id']}={p['liquidity']}:{p['lower']}:"
                         f"{p['upper']}:{int(inside)}:{tick // MARKER_TICK_BUCKET}")
        return out, marks

    async def _resolve_pools(self, chain: str, entry, parsed: list[dict]) -> None:
        """Ask the factory once per (pair, fee) and remember the answer."""
        signature = ("getPool(address,address,int24)" if entry.kind == "slipstream"
                     else "getPool(address,address,uint24)")
        (factory,) = await self._adapter.eth_call_many(
            chain, [(entry.address, selector("factory()"))])
        factory = decode_address(factory)
        wanted = []
        for p in parsed:
            key = (chain, entry.kind, p["token0"], p["token1"], p["key4"])
            p["pool_key"] = key
            if key not in self._pools and key not in wanted:
                wanted.append(key)
        if wanted and factory:
            answers = await self._adapter.eth_call_many(chain, [
                (factory, selector(signature)
                 + f"{int(k[2], 16):064x}{int(k[3], 16):064x}{k[4]:064x}")
                for k in wanted])
            for key, answer in zip(wanted, answers):
                found = decode_address(answer)
                if found:
                    self._pools[key] = found.lower()
                else:
                    log.warning("%s: no pool for %s", entry.label, key[2:])
        for p in parsed:
            p["pool"] = self._pools.get(p["pool_key"], "")

    async def _slots(self, chain: str, parsed: list[dict]) -> dict[str, tuple[int, int]]:
        pools = sorted({p["pool"] for p in parsed if p["pool"]})
        answers = await self._adapter.eth_call_many(
            chain, [(pool, selector("slot0()")) for pool in pools])
        out = {}
        for pool, answer in zip(pools, answers):
            price = _word(answer, 0)
            if price:
                out[pool] = (price, _signed(_word(answer, 1)))
        return out

    async def _resolve_meta(self, chain: str, parsed: list[dict]) -> None:
        wanted = []
        for p in parsed:
            for contract in (p["token0"], p["token1"]):
                if (chain, contract) not in self._meta and contract not in wanted:
                    wanted.append(contract)
        if not wanted:
            return
        calls = []
        for contract in wanted:
            calls += [(contract, selector("symbol()")),
                      (contract, selector("decimals()"))]
        answers = await self._adapter.eth_call_many(chain, calls)
        for i, contract in enumerate(wanted):
            symbol = decode_string(answers[i * 2]) or ""
            decimals = decode_uint(answers[i * 2 + 1])
            self._meta[(chain, contract)] = (symbol, 18 if decimals is None else decimals)
