"""Uniswap v4 positions.

The same idea as v3 - liquidity between two ticks, held as an ERC-721 - and
almost none of the same plumbing. Four differences decide this module's shape,
and each was checked against mainnet before it was written down:

  * **One contract holds every pool.** There is no factory and no pool contract,
    so there is no `slot0()` to call: `PoolManager.slot0()` reverts. Pool state
    is read out of storage with `extsload`, at a slot computed here.
  * **A pool is five fields, not three.** PoolKey is (currency0, currency1, fee,
    tickSpacing, hooks); fee and tick spacing are independent, where v3 tied
    them together, and a hook contract is part of the pool's identity. The pool
    id is the hash of that struct, which means it needs no lookup call at all.
  * **The position NFT is not enumerable.** `supportsInterface(ERC721Enumerable)`
    answers 0 and `tokenOfOwnerByIndex` reverts, so a wallet's positions cannot
    be listed by asking the contract - see EvmAdapter.owned_nfts for what is
    done instead, and why it is not `eth_getLogs`.
  * **Native ETH is a currency.** `currency0` may be the zero address and mean
    real ETH; asking it for `symbol()` reverts.

What does carry over is the arithmetic: v4 keeps v3's tick scale and the same
amount formulas, so protocols/ticks.py is reused unchanged. It was verified
against two live v4 pools - the price read out of `extsload` satisfies the same
bracket the module is tested on.

Uncollected fees are deliberately not reported yet. v3 could be asked, by
simulating `collect()`; v4 collects through the unlock pattern, so the only way
to know is to compute fee growth from several more storage slots. That is real
arithmetic with nothing to check it against, and this file would rather report
nothing than report a number nobody verified.
"""

import logging

from eth_utils import keccak

from ..chains.abi import decode_address, decode_uint, decode_string, selector
from ..chains.base import Target
from ..chains.evm import EvmAdapter
from ..models import Position
from . import ticks as tickmath

log = logging.getLogger(__name__)

POOLS_SLOT = 6
"""Where PoolManager keeps its pools mapping.

A storage slot is not an interface, and this is the one number here that could
change under a redeploy without any call failing - the read would simply return
another slot's contents. `catalog-check` guards it: the price it decodes has to
satisfy ratio(tick) <= sqrtPriceX96 < ratio(tick+1), which noise does not.
"""

MARKER_TICK_BUCKET = 60          # same reasoning as univ3.MARKER_TICK_BUCKET
NATIVE = "0x" + "0" * 40


def _signed(value: int, bits: int = 24) -> int:
    sign = 1 << (bits - 1)
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & sign else value


def _word(data, index: int) -> int | None:
    if not isinstance(data, str) or not data.startswith("0x"):
        return None
    body = bytes.fromhex(data[2:])
    if len(body) < 32 * (index + 1):
        return None
    return int.from_bytes(body[index * 32:(index + 1) * 32], "big")


def pool_id(currency0: int, currency1: int, fee: int, spacing: int, hooks: int) -> bytes:
    """keccak256(abi.encode(PoolKey)) - the pool's identity, computed not asked.

    v3 needed a call to the factory to turn a pair and a fee into a pool
    address. Here the id is a hash of the key, so the whole lookup is local.
    """
    packed = b"".join(x.to_bytes(32, "big") for x in (
        currency0, currency1, fee, spacing & ((1 << 256) - 1), hooks))
    return keccak(packed)


def state_slot(pid: bytes) -> str:
    """Where that pool's slot0 lives inside PoolManager's storage."""
    return "0x" + keccak(pid + POOLS_SLOT.to_bytes(32, "big")).hex()


def unpack_slot0(word: int | None) -> tuple[int, int] | None:
    """sqrtPriceX96 in the low 160 bits, the tick in the next 24."""
    if not word:
        return None
    price = word & ((1 << 160) - 1)
    if not price:
        return None
    return price, _signed(word >> 160)


def unpack_info(word: int) -> tuple[int, int, int]:
    """PositionInfo: poolId(200) | tickUpper(24) | tickLower(24) | subscriber(8).

    The stored poolId is truncated to its top 200 bits, which makes it a free
    check on the key we hashed ourselves rather than dead weight.
    """
    return _signed(word >> 8), _signed(word >> 32), word >> 56


class UniV4Source:
    name = "univ4"

    def __init__(self, api_key: str = "", entries=None, adapter: EvmAdapter | None = None):
        self._entries = entries or []
        self._adapter = adapter or EvmAdapter(api_key)
        self._own = adapter is None
        self._managers: dict[tuple[str, str], str] = {}
        self._meta: dict[tuple[str, str], tuple[str, int]] = {}

    async def aclose(self) -> None:
        if self._own:
            await self._adapter.aclose()

    def _on(self, chain: str):
        return [e for e in self._entries if e.chain == chain and e.kind == "univ4"]

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

    async def _pool_manager(self, chain: str, entry) -> str | None:
        key = (chain, entry.address)
        if key not in self._managers:
            (answer,) = await self._adapter.eth_call_many(
                chain, [(entry.address, selector("poolManager()"))])
            found = decode_address(answer)
            if not found:
                log.warning("%s: position manager named no pool manager", entry.label)
                return None
            self._managers[key] = found.lower()
        return self._managers[key]

    async def _market(self, t: Target, chain: str, entry):
        ids = await self._adapter.owned_nfts(chain, t.address, entry.address)
        if not ids:
            return [], []
        manager = await self._pool_manager(chain, entry)
        if manager is None:
            return [], []

        call = self._adapter.eth_call_many
        liquidity = [decode_uint(a) for a in await call(
            chain, [(entry.address, selector("getPositionLiquidity(uint256)")
                     + f"{i:064x}") for i in ids])]
        live = [i for i, amount in zip(ids, liquidity) if amount]
        amounts = {i: a for i, a in zip(ids, liquidity) if a}
        if not live:
            return [], []

        infos = await call(chain, [
            (entry.address, selector("getPoolAndPositionInfo(uint256)") + f"{i:064x}")
            for i in live])

        parsed = []
        for token_id, answer in zip(live, infos):
            if _word(answer, 5) is None:
                continue
            currency0, currency1 = _word(answer, 0), _word(answer, 1)
            fee, spacing = _word(answer, 2), _signed(_word(answer, 3))
            hooks = _word(answer, 4)
            lower, upper, stored_id = unpack_info(_word(answer, 5))
            pid = pool_id(currency0, currency1, fee, spacing, hooks)
            if int.from_bytes(pid, "big") >> 56 != stored_id:
                # The NFT stores the top 200 bits of the pool id it belongs to.
                # Disagreeing means the key was assembled wrongly, and every
                # number that follows would be read from another pool's storage.
                log.warning("%s: position %s does not match the pool key it names",
                            entry.label, token_id)
                continue
            parsed.append({
                "id": token_id, "pid": pid,
                "token0": f"0x{currency0:040x}", "token1": f"0x{currency1:040x}",
                "lower": lower, "upper": upper, "hooks": f"0x{hooks:040x}",
                "liquidity": amounts[token_id]})
        if not parsed:
            return [], []

        slots = sorted({state_slot(p["pid"]) for p in parsed})
        answers = await call(chain, [(manager, selector("extsload(bytes32)") + s[2:])
                                     for s in slots])
        state = {}
        for slot, answer in zip(slots, answers):
            got = unpack_slot0(decode_uint(answer))
            if got:
                state[slot] = got

        await self._resolve_meta(chain, parsed)

        out, marks = [], []
        for p in parsed:
            got = state.get(state_slot(p["pid"]))
            if got is None:
                log.warning("%s: no pool state for position %s", entry.label, p["id"])
                continue
            sqrt_price, tick = got
            amount0, amount1 = tickmath.amounts_for_liquidity(
                sqrt_price, p["lower"], p["upper"], p["liquidity"])
            inside = tickmath.in_range(tick, p["lower"], p["upper"])
            for leg, (contract, amount) in enumerate(
                    ((p["token0"], amount0), (p["token1"], amount1))):
                symbol, decimals = self._meta.get((chain, contract), ("", 18))
                native = contract == NATIVE
                out.append(Position(
                    protocol=self.name,
                    key=f"{chain}:{p['id']}:{leg}",
                    symbol="ETH" if native else symbol,
                    amount_raw=amount,
                    decimals=18 if native else decimals,
                    asset_key=(f"{chain}:native" if native else f"{chain}:{contract}"),
                    accrues=True,
                    extra={"venue": entry.protocol, "token_id": str(p["id"]),
                           "pool": "0x" + p["pid"].hex(), "hooks": p["hooks"],
                           "tick": str(tick), "tick_lower": str(p["lower"]),
                           "tick_upper": str(p["upper"]),
                           "in_range": "true" if inside else "false"}))
            marks.append(f"{chain}:{p['id']}={p['liquidity']}:{p['lower']}:"
                         f"{p['upper']}:{int(inside)}:{tick // MARKER_TICK_BUCKET}")
        return out, marks

    async def _resolve_meta(self, chain: str, parsed: list[dict]) -> None:
        wanted = []
        for p in parsed:
            for contract in (p["token0"], p["token1"]):
                # The zero address is ETH itself and answers no ERC-20 method.
                if contract != NATIVE and (chain, contract) not in self._meta \
                        and contract not in wanted:
                    wanted.append(contract)
        if not wanted:
            return
        calls = []
        for contract in wanted:
            calls += [(contract, selector("symbol()")),
                      (contract, selector("decimals()"))]
        answers = await self._adapter.eth_call_many(chain, calls)
        for i, contract in enumerate(wanted):
            decimals = decode_uint(answers[i * 2 + 1])
            self._meta[(chain, contract)] = (decode_string(answers[i * 2]) or "",
                                             18 if decimals is None else decimals)
