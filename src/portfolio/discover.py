"""Propose whitelist candidates: what the watched addresses actually hold.

Run by hand, rarely, and never by the tick. The output is a YAML block for a
human to paste into `tokens:` - nothing here writes to the config.

Two rules keep this from undoing the whitelist it feeds:

  * CoinGecko listing is the filter, not the dollar value. A scam token's price
    comes from its own faked pool, so a "> $1" test is one it passes by
    construction while harmless dust fails it. Being listed by contract address
    is the part that cannot be forged for free.
  * DexScreener is never consulted here. It is the right last resort for pricing
    a token you already approved, and the wrong way to decide whether to approve
    one.
"""

import logging
from decimal import Decimal

from . import catalog
from .chains import abi
from .chains.base import Target
from .chains.evm import EvmAdapter

log = logging.getLogger(__name__)

CG_CHUNK = 100                             # contracts per CoinGecko request
NATIVE_DECIMALS = 18                       # every native coin on these networks


def _targets(cfg) -> list[Target]:
    return [Target(address=a.address, label=a.label, watch=frozenset(a.watch),
                   chains=tuple(a.chains))
            for a in cfg.addresses
            if a.enabled and a.chain == "evm" and "tokens" in a.watch]


async def collect(cfg, adapter: EvmAdapter, sources, min_usd: float,
                  include_unpriced: bool = False) -> tuple[list[dict], list[dict]]:
    """(candidates, unpriced): held tokens worth at least min_usd, richest first,
    and - when asked - what is held but cannot be valued at all."""
    # (chain, contract) -> {label: raw amount}
    held: dict[tuple[str, str], dict[str, int]] = {}
    for target in _targets(cfg):
        for scope in adapter.scopes(target):
            balances = await adapter.held_tokens(target, scope)
            for contract, raw in balances.items():
                held.setdefault((scope, contract), {})[target.label] = raw
            log.info("%s/%s: %d tokens held", target.label, scope, len(balances))

    by_chain: dict[str, list[str]] = {}
    for chain, contract in held:
        by_chain.setdefault(chain, []).append(contract)

    prices: dict[tuple[str, str], float] = {}
    for chain, contracts in by_chain.items():
        for i in range(0, len(contracts), CG_CHUNK):
            chunk = contracts[i:i + CG_CHUNK]
            try:
                found = await sources.coingecko_tokens(chain, chunk)
            except Exception as e:                       # noqa: BLE001
                log.warning("coingecko failed for %s: %s", chain, e)
                continue
            prices.update({(chain, c): p for c, p in found.items()})
        log.info("%s: %d held, %d listed on coingecko", chain, len(contracts),
                 sum(1 for c in contracts if (chain, c) in prices))

    # Metadata costs a call per token, so by default ask only about the ones
    # with a price: without one there is nothing to compare against min_usd.
    # --show-unpriced knowingly pays for the rest.
    meta: dict[tuple[str, str], dict] = {}
    for chain in by_chain:
        wanted = [c for c in by_chain[chain]
                  if include_unpriced or (chain, c) in prices]
        if wanted:
            got = await adapter.token_metadata(chain, wanted)
            meta.update({(chain, c): m for c, m in got.items()})

    known = {(t.chain, t.contract) for t in cfg.tokens}
    rows, unpriced = [], []
    for (chain, contract), per_label in held.items():
        price = prices.get((chain, contract))
        decimals = (meta.get((chain, contract)) or {}).get("decimals")
        if price is None:
            if include_unpriced and decimals is not None:
                m = meta[(chain, contract)]
                unpriced.append({
                    "chain": chain, "contract": contract,
                    "symbol": ((m.get("symbol") or "?").strip()),
                    "decimals": int(decimals),
                    "holders": {k: float(Decimal(v) / (10 ** int(decimals)))
                                for k, v in per_label.items()},
                    "known": (chain, contract) in known})
            continue
        if decimals is None:
            continue
        decimals = int(decimals)
        amounts = {label: Decimal(raw) / (10 ** decimals)
                   for label, raw in per_label.items()}
        usd = float(sum(amounts.values())) * price
        if usd < min_usd:
            continue
        rows.append({
            "chain": chain, "contract": contract, "decimals": decimals,
            "symbol": ((meta[(chain, contract)].get("symbol") or "?").strip()),
            "price": price, "usd": usd,
            "holders": {k: float(v) for k, v in amounts.items()},
            "known": (chain, contract) in known,
        })
    rows.sort(key=lambda r: -r["usd"])
    unpriced.sort(key=lambda r: (r["chain"], r["symbol"].lower()))
    return rows, unpriced


def render(rows: list[dict], min_usd: float,
           unpriced: list[dict] | None = None) -> str:
    if not rows and not unpriced:
        return f"Nothing held is both listed on CoinGecko and worth ${min_usd:g}+."

    lines = [f"{'chain':9} {'symbol':14} {'value':>12}  holdings",
             "-" * 64]
    for r in rows:
        who = ", ".join(f"{k} {v:,.6f}".rstrip("0").rstrip(".")
                        for k, v in r["holders"].items())
        tail = "  [already in config]" if r["known"] else ""
        lines.append(f"{r['chain']:9} {r['symbol']:14} "
                     f"{r['usd']:12,.2f}  {who}{tail}")

    fresh = [r for r in rows if not r["known"]]
    lines.append(f"\n{len(rows)} candidates over ${min_usd:g}, "
                 f"{len(fresh)} not yet whitelisted, "
                 f"${sum(r['usd'] for r in fresh):,.2f} unwatched")

    if fresh:
        lines.append("\nPaste into `tokens:` what you actually want watched:\n")
        for r in fresh:
            lines.append(f"  - chain: {r['chain']}")
            lines.append(f'    contract: "{r["contract"]}"')
            lines.append(f"    symbol: {r['symbol']}")
            lines.append(f"    decimals: {r['decimals']}   # ${r['usd']:,.2f}")
            lines.append("")

    if unpriced:
        # No price means no ranking: a real position and an airdrop look alike
        # here, which is why this needs human eyes and is off by default.
        lines.append(f"\nHeld but unpriced ({len(unpriced)}). CoinGecko lists none "
                     f"of these contracts, and discovery never asks a DEX pool,")
        lines.append("so there is nothing to rank them by - read them yourself:\n")
        for r in unpriced:
            amount = ", ".join(f"{k} {v:,.8f}".rstrip("0").rstrip(".")
                               for k, v in r["holders"].items())
            tail = "  [already in config]" if r["known"] else ""
            lines.append(f"  {r['chain']:9} {r['symbol'][:20]:20} "
                         f"{r['contract']}  {amount}{tail}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Positions the token index cannot see (PLAN §6 blind spots, §14 catalog).
# --------------------------------------------------------------------------

async def _receipts(adapter: EvmAdapter, chain: str,
                    entries: list[catalog.Entry]) -> dict[str, dict]:
    """protocol/chain label -> {kind, tokens: [{symbol, contract, debt}]}.

    Three round trips of batches for the whole network, whatever the protocol
    count: the entry points are asked together, then the enumerations they point
    at, then the per-reserve lookup that names the debt tokens. Nothing here
    depends on an address, so the result is computed once and reused for every
    wallet.

    Debt is included because a variable debt token is an ordinary ERC-20 and
    therefore free to watch - it rides the same probe as everything else. Only
    Aave-shaped markets offer it here; a Compound v2 fork keeps a borrow in
    `borrowBalanceStored(address)`, which is a call rather than a balance and
    has nowhere to sit in the whitelist.
    """
    aave = [e for e in entries if e.kind == "aave_v3"]
    comp = [e for e in entries if e.kind == "compound_v2"]

    step1 = ([(e.address, abi.selector("getPoolDataProvider()")) for e in aave]
             + [(e.address, abi.selector("getAllMarkets()")) for e in comp])
    answers = await adapter.eth_call_many(chain, step1)
    providers = [abi.decode_address(a) for a in answers[:len(aave)]]
    markets = [abi.decode_address_array(a) or [] for a in answers[len(aave):]]

    out: dict[str, dict] = {}

    live = [(e, p) for e, p in zip(aave, providers) if p]
    for entry, provider in zip(aave, providers):
        if provider is None:
            log.warning("%s: no pool data provider; catalog address may be stale",
                        entry.label)

    step2 = ([(p, abi.selector("getAllATokens()")) for _, p in live]
             + [(p, abi.selector("getAllReservesTokens()")) for _, p in live])
    listed = await adapter.eth_call_many(chain, step2) if step2 else []

    # One call per reserve names its aToken and its two debt tokens. Batched
    # across every market on the network, and never repeated per address.
    step3, owners = [], []
    for i, (entry, provider) in enumerate(live):
        reserves = abi.decode_symbol_address_array(listed[len(live) + i]) or []
        for _, underlying in reserves:
            step3.append((provider, abi.encode_address(
                "getReserveTokensAddresses(address)", underlying)))
            owners.append(entry.label)
    debts = await adapter.eth_call_many(chain, step3) if step3 else []

    per_label: dict[str, list[dict]] = {}
    for label, answer in zip(owners, debts):
        # (aToken, stableDebtToken, variableDebtToken) - the third is the one
        # that carries a modern borrow.
        variable = abi.decode_address_at(answer, 2)
        if variable:
            per_label.setdefault(label, []).append(
                {"symbol": "", "contract": variable.lower(), "debt": True})

    for i, (entry, _) in enumerate(live):
        tokens = abi.decode_symbol_address_array(listed[i]) or []
        if not tokens:
            log.warning("%s: the market listed no receipt tokens", entry.label)
        out[entry.label] = {"kind": entry.kind, "tokens": (
            [{"symbol": s, "contract": a.lower(), "debt": False} for s, a in tokens]
            + per_label.get(entry.label, []))}

    for entry, found in zip(comp, markets):
        if not found:
            log.warning("%s: the comptroller listed no markets", entry.label)
        # Symbols are not part of getAllMarkets(); they are asked of the few
        # markets that turn out to hold something, rather than of all 55.
        out[entry.label] = {"kind": entry.kind, "tokens": [
            {"symbol": "", "contract": a.lower(), "debt": False} for a in found]}
    return out


async def collect_protocols(cfg, adapter: EvmAdapter,
                            entries: list[catalog.Entry] | None = None
                            ) -> list[dict]:
    """Catalog positions the watched addresses actually hold.

    Cost is one balance sweep per address per network - an Aave market alone
    lists 67 receipt tokens, so this is a manual command and never a tick.
    """
    entries = catalog.load() if entries is None else entries
    per_chain = catalog.by_chain(entries)
    targets = _targets(cfg)
    known = {(t.chain, t.contract) for t in cfg.tokens}

    # (chain, contract) -> row being built
    rows: dict[tuple[str, str], dict] = {}
    listings: dict[str, dict[str, dict]] = {}

    for target in targets:
        for chain in adapter.scopes(target):
            on_chain = per_chain.get(chain)
            if not on_chain:
                continue
            if chain not in listings:
                listings[chain] = await _receipts(adapter, chain, on_chain)
                total = sum(len(v["tokens"]) for v in listings[chain].values())
                log.info("%s: %d receipt tokens across %d protocols",
                         chain, total, len(listings[chain]))

            flat = [(label, found["kind"], tok)
                    for label, found in listings[chain].items()
                    for tok in found["tokens"]]
            held = await adapter.balances_of(
                chain, target.address, [tok["contract"] for _, _, tok in flat])

            for label, kind, tok in flat:
                contract = tok["contract"]
                raw = held.get(contract)
                if not raw:
                    continue
                row = rows.setdefault((chain, contract), {
                    "chain": chain, "contract": contract, "protocol": label,
                    "kind": kind, "symbol": tok["symbol"], "debt": tok["debt"],
                    "holders": {}, "known": (chain, contract) in known})
                row["holders"][target.label] = raw
            log.info("%s/%s: swept %d receipt tokens", target.label, chain, len(flat))

    await _describe(adapter, rows)
    return sorted(rows.values(), key=lambda r: (r["chain"], r["protocol"], r["symbol"]))


async def _describe(adapter: EvmAdapter, rows: dict[tuple[str, str], dict]) -> None:
    """Fill in symbol, decimals and the underlying, for the held ones only.

    Three calls per position and none per catalog entry: whether a market has
    fifty listings is irrelevant once the balance sweep has narrowed them to the
    two an address actually holds.
    """
    by_chain: dict[str, list[dict]] = {}
    for row in rows.values():
        by_chain.setdefault(row["chain"], []).append(row)

    for chain, group in by_chain.items():
        calls = []
        for row in group:
            c = row["contract"]
            calls += [(c, abi.selector("symbol()")),
                      (c, abi.selector("decimals()")),
                      # Aave names it one way, Compound another; asking both
                      # costs a slot in a batch and saves a branch here.
                      (c, abi.selector("UNDERLYING_ASSET_ADDRESS()")),
                      (c, abi.selector("underlying()"))]
        answers = await adapter.eth_call_many(chain, calls)
        for i, row in enumerate(group):
            symbol, decimals, aave_under, comp_under = answers[i * 4:i * 4 + 4]
            row["symbol"] = row["symbol"] or abi.decode_string(symbol) or ""
            row["decimals"] = abi.decode_uint(decimals)
            row["underlying"] = (abi.decode_address(aave_under)
                                 or abi.decode_address(comp_under))

        # The underlying's own decimals are what a cToken position is measured
        # in; the cToken's own 8 describe the share, not the money.
        wants = [r for r in group if r.get("underlying")]
        if wants:
            und = await adapter.eth_call_many(
                chain, [(r["underlying"], abi.selector("decimals()")) for r in wants])
            for row, answer in zip(wants, und):
                row["underlying_decimals"] = abi.decode_uint(answer)


def _is_share(row: dict) -> bool:
    """True for a cToken: eight decimals of share against a rate, not a balance.

    An aToken rebases and reads one-to-one with the asset, so its balance is the
    position. A cToken's balance stays put while exchangeRateStored() climbs -
    the same shape `rate_call` was built for, with the rate scaled by 1e18.

    Read from the catalog kind rather than guessed from the decimals. The guess
    ("eight decimals over a different underlying") looks sound and then meets
    a market for the native coin, which has no underlying() to compare against:
    Venus's vBNB was proposed as a plain rebasing balance, which would have
    counted eight decimals of share as if they were BNB.
    """
    return row.get("kind") == "compound_v2"


def render_protocols(rows: list[dict]) -> str:
    if not rows:
        return ("Nothing found. The catalog covers "
                f"{len({e.protocol for e in catalog.load()})} protocols on "
                f"{len(catalog.by_chain())} networks; a position outside it, or "
                "held through a contract that does not answer balanceOf, is "
                "invisible here.")

    lines = [f"{'chain':9} {'protocol':16} {'symbol':18} holders", "-" * 72]
    for r in rows:
        decimals = r.get("decimals")
        who = ", ".join(
            f"{k} {Decimal(v) / (10 ** decimals):,.6f}".rstrip("0").rstrip(".")
            if decimals is not None else f"{k} {v} raw"
            for k, v in r["holders"].items())
        tail = "  [already in config]" if r["known"] else ""
        owed = "owed  " if r.get("debt") else ""
        lines.append(f"{r['chain']:9} {r['protocol'].split('/')[0]:16} "
                     f"{(r['symbol'] or '?'):18} {owed}{who}{tail}")

    fresh = [r for r in rows if not r["known"] and r.get("decimals") is not None]
    lines.append(f"\n{len(rows)} positions found, {len(fresh)} not yet whitelisted.")
    if not fresh:
        return "\n".join(lines)

    lines.append("\nPaste into `tokens:` what you want watched. Fill in "
                 "coingecko_id yourself:")
    lines.append("a receipt token is listed nowhere by its own address, so the "
                 "id has to name")
    lines.append("the asset it redeems for - the underlying is given beside "
                 "each entry.\n")
    for r in fresh:
        share = _is_share(r)
        # A cToken market for the native coin has no underlying() to ask; every
        # native coin on the networks here has eighteen decimals.
        decimals = (r.get("underlying_decimals") or NATIVE_DECIMALS) if share \
            else r["decimals"]
        underlying = r.get("underlying") or ("the native coin" if share else "?")
        lines.append(f"  - chain: {r['chain']}")
        lines.append(f'    contract: "{r["contract"]}"')
        lines.append(f"    symbol: {r['symbol'] or '?'}")
        lines.append(f"    decimals: {decimals}")
        lines.append(f"    coingecko_id:        # underlying: {underlying}")
        if r.get("debt"):
            # Owed, not held. The id above is not optional for these: interest
            # is told from a borrow by what the change is worth.
            lines.append("    debt: true")
        elif share:
            # The balance is shares; the value lives in the rate, and the rate
            # is scaled by 1e18 whatever the underlying's own decimals are.
            lines.append('    rate_call: "exchangeRateStored()"')
            lines.append("    share_decimals: 18")
        else:
            lines.append("    yield_bearing: true")
        lines.append(f"    # {r['protocol']}")
        lines.append("")
    return "\n".join(lines)


NFT_KINDS = ("univ3", "slipstream")


async def position_managers(adapter: EvmAdapter, chain: str,
                            entries: list[catalog.Entry]) -> dict[str, str]:
    """label -> the factory each position manager points at, or "" if silent.

    A position manager has no receipt tokens to enumerate, so it cannot be
    checked the way a lending market is. Asking it for its factory is the
    cheapest question only a real one answers.
    """
    wanted = [e for e in entries if e.kind in NFT_KINDS]
    if not wanted:
        return {}
    answers = await adapter.eth_call_many(
        chain, [(e.address, abi.selector("factory()")) for e in wanted])
    return {e.label: (abi.decode_address(a) or "")
            for e, a in zip(wanted, answers)}


async def collect_nfts(cfg, adapter: EvmAdapter,
                       entries: list[catalog.Entry] | None = None) -> list[dict]:
    """Which addresses hold concentrated-liquidity positions, and how many.

    Nothing here is proposed for `tokens:` - a range position is not a token and
    cannot be whitelisted; what it needs is `univ3` in the address's `watch`
    list, which is what the report says.

    Counting the NFTs is not enough to say that. Withdrawing all the liquidity
    from a position leaves the NFT sitting in the wallet with nothing in it, and
    an address whose five positions are all closed reads as five positions until
    somebody asks each one. So each is asked.
    """
    entries = catalog.load() if entries is None else entries
    per_chain = catalog.by_chain([e for e in entries if e.kind in NFT_KINDS])
    rows = []
    for target in _targets(cfg):
        for chain in adapter.scopes(target):
            for entry in per_chain.get(chain, []):
                (answer,) = await adapter.eth_call_many(chain, [(
                    entry.address,
                    abi.encode_address("balanceOf(address)", target.address))])
                held = abi.decode_uint(answer) or 0
                if not held:
                    continue
                ids = [abi.decode_uint(x) for x in await adapter.eth_call_many(
                    chain, [(entry.address,
                             abi.selector("tokenOfOwnerByIndex(address,uint256)")
                             + f"{int(target.address, 16):064x}{i:064x}")
                            for i in range(held)])]
                states = await adapter.eth_call_many(
                    chain, [(entry.address, abi.selector("positions(uint256)")
                             + f"{i:064x}") for i in ids if i is not None])
                live = sum(1 for s in states if _liquidity(s))
                rows.append({"chain": chain, "protocol": entry.protocol,
                             "label": target.label, "count": held, "live": live,
                             "watched": "univ3" in target.watch})
    rows.sort(key=lambda r: (r["chain"], r["protocol"], r["label"]))
    return rows


def _liquidity(answer) -> int:
    """Word 7 of positions(): the liquidity coefficient, zero once withdrawn."""
    body = answer if isinstance(answer, str) else ""
    if not body.startswith("0x") or len(body) < 2 + 64 * 8:
        return 0
    return int(body[2 + 64 * 7:2 + 64 * 8], 16)


def render_nfts(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = ["", "Concentrated liquidity (not a token; cannot be whitelisted):"]
    for r in rows:
        empty = r["count"] - r["live"]
        note = f", {empty} closed but not burned" if empty else ""
        tail = "" if r["watched"] or not r["live"] else "   <- not watched"
        lines.append(f"  {r['label']:12} {r['chain']:9} {r['protocol']:22} "
                     f"{r['live']} with liquidity{note}{tail}")
    unwatched = {r["label"] for r in rows if r["live"] and not r["watched"]}
    if unwatched:
        lines.append("")
        lines.append(f"Add `univ3` to the watch list of: {', '.join(sorted(unwatched))}")
        lines.append("The amounts are computed from the pool price, so nothing "
                     "goes in `tokens:`.")
    return "\n".join(lines)
