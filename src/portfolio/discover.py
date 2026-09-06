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

from .chains.base import Target
from .chains.evm import EvmAdapter

log = logging.getLogger(__name__)

CG_CHUNK = 100                             # contracts per CoinGecko request


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
