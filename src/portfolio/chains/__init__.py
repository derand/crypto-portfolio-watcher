from .base import AddressState, ChainAdapter, Cursor, Target
from .bitcoin import BitcoinAdapter
from .evm import EvmAdapter
from .withdrawals import BeaconWithdrawals

__all__ = ["AddressState", "ChainAdapter", "Cursor", "Target",
           "BitcoinAdapter", "EvmAdapter", "BeaconWithdrawals", "build_adapters"]


def build_adapters(cfg) -> dict:
    """chain name -> adapter. Adding a chain is one line here plus one module."""
    adapters = {"bitcoin": BitcoinAdapter()}
    if cfg.api_keys.alchemy:
        # Withdrawals come from Etherscan because they are not transactions and
        # Alchemy's transfer API cannot see them at all.
        wd = (BeaconWithdrawals(cfg.api_keys.etherscan)
              if any("beacon" in a.watch for a in cfg.addresses if a.enabled) else None)
        adapters["evm"] = EvmAdapter(cfg.api_keys.alchemy, cfg.tokens_by_chain(),
                                     withdrawals=wd)
    return adapters


def missing_sources(cfg) -> list[str]:
    """Enabled addresses that no adapter can reach.

    Without this an EVM address plus a forgotten API key means `scan` reports
    "probed 0" and moves on - the exact silent success that makes a watcher
    useless. Callers surface these; nothing is skipped quietly.
    """
    from ..protocols import build_sources

    have = set(build_adapters(cfg))
    sources = set(build_sources(cfg))
    problems = []
    for a in cfg.addresses:
        if not a.enabled or a.chain in have:
            continue
        if set(a.watch) <= sources:
            continue        # nothing on-chain to poll; a protocol source covers it
        why = ("ALCHEMY_API_KEY is not set" if a.chain == "evm"
               else f"no adapter for {a.chain}")
        problems.append(f"{a.label} ({a.chain}): {why}")
    return problems
