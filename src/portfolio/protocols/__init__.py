from .base import PositionSource
from .beacon import BeaconSource
from .hyperliquid import HyperliquidSource

__all__ = ["PositionSource", "BeaconSource", "HyperliquidSource",
           "PROTOCOL_SOURCES", "build_sources"]

PROTOCOL_SOURCES = {HyperliquidSource.name, BeaconSource.name}
"""Watch keywords a position source answers to.

Derived from the classes rather than typed out, so config validation cannot
drift from what is actually registered below - and so the allowed set names
nothing this repository does not implement. `watch` used to carry entries no
build_sources() line ever read; a keyword with no reader is not a feature, it
is a statement about whoever wrote the config.
"""


def build_sources(cfg) -> dict:
    """watch keyword -> position source. One line per protocol."""
    sources = {"hyperliquid": HyperliquidSource()}
    validators = cfg.validators_by_address()
    if validators:
        sources["beacon"] = BeaconSource(validators)
    return sources
