from .base import PositionSource
from .beacon import BeaconSource
from .hyperliquid import HyperliquidSource
from .univ3 import UniV3Source
from .univ4 import UniV4Source

__all__ = ["PositionSource", "BeaconSource", "HyperliquidSource", "UniV3Source",
           "UniV4Source", "PROTOCOL_SOURCES", "CHAIN_SCOPED_SOURCES",
           "build_sources"]

PROTOCOL_SOURCES = {HyperliquidSource.name, BeaconSource.name, UniV3Source.name,
                    UniV4Source.name}
"""Watch keywords a position source answers to.

Derived from the classes rather than typed out, so config validation cannot
drift from what is actually registered below - and so the allowed set names
nothing this repository does not implement. `watch` used to carry entries no
build_sources() line ever read; a keyword with no reader is not a feature, it
is a statement about whoever wrote the config.
"""


CHAIN_SCOPED_SOURCES = {UniV3Source.name, UniV4Source.name}
"""Sources that read one network at a time and so need the address's `chains`.

Hyperliquid and the beacon chain are each one place; a Uniswap position lives on
a particular network, and an address watching it with no `chains` would be
polled nowhere at all - silently, which is the worst way to watch nothing.
"""


def build_sources(cfg) -> dict:
    """watch keyword -> position source. One line per protocol."""
    from .. import catalog

    sources = {"hyperliquid": HyperliquidSource()}
    validators = cfg.validators_by_address()
    if validators:
        sources["beacon"] = BeaconSource(validators)
    if cfg.api_keys.alchemy:
        entries = catalog.load()
        for source in (UniV3Source, UniV4Source):
            if any(source.name in a.watch for a in cfg.addresses if a.enabled):
                sources[source.name] = source(cfg.api_keys.alchemy, entries)
    return sources
