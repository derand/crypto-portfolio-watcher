import httpx
import pytest

from portfolio.chains.base import Target
from portfolio.protocols.beacon import BeaconSource

ME = "0xbeac017a3c9d4e5f6a7b8c9d0e1f2a3b4c5d6e7f"
TARGET = Target(address=ME, label="stake-1", watch=frozenset({"beacon"}))


def validator(index=999999, balance_gwei=32008617079, status="active_ongoing",
              slashed=False, effective=32000000000):
    return {"index": str(index), "balance": str(balance_gwei), "status": status,
            "validator": {"slashed": slashed, "effective_balance": str(effective)}}


def source(rows, validators=None):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"data": rows})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # `or` would swallow an intentionally empty mapping, which is exactly the
    # case one of these tests is about.
    if validators is None:
        validators = {ME: [999999]}
    return BeaconSource(validators, url="http://beacon", client=client), seen


async def test_balance_is_gwei_and_reported_as_eth():
    """The beacon API answers in gwei. Reporting it unscaled would value one
    validator at three hundred billion ETH."""
    s, _ = source([validator()])
    positions, _ = await s.fetch(TARGET)
    assert len(positions) == 1
    p = positions[0]
    assert p.amount_raw == 32008617079 * 10**9
    assert (p.decimals, p.symbol, p.key) == (18, "ETH", "validator:999999")
    assert p.accrues is True, "a validator earns every epoch; that is not an event"
    assert p.extra["status"] == "active_ongoing"


async def test_all_validators_are_asked_for_in_one_request():
    s, seen = source([validator(1), validator(2)], validators={ME: [1, 2]})
    positions, _ = await s.fetch(TARGET)
    assert [p.key for p in positions] == ["validator:1", "validator:2"]
    assert len(seen) == 1 and seen[0]["id"] == "1,2"


async def test_an_address_without_validators_asks_nothing():
    s, seen = source([], validators={})
    assert await s.fetch(TARGET) == ([], "")
    assert seen == []


async def test_marker_ignores_epoch_drift_but_never_a_status_change():
    """The balance ticks every epoch. Carried exactly it would make every tick
    look busy; carried too coarsely an exit would be rounded away."""
    a, _ = source([validator(balance_gwei=32008617079)])
    b, _ = source([validator(balance_gwei=32008617999)])       # +0.0000009 ETH
    c, _ = source([validator(balance_gwei=32011000000)])       # +0.0024 ETH
    d, _ = source([validator(balance_gwei=32008617079, status="exited_unslashed")])

    _, m_a = await a.fetch(TARGET)
    _, m_b = await b.fetch(TARGET)
    _, m_c = await c.fetch(TARGET)
    _, m_d = await d.fetch(TARGET)
    assert m_a == m_b, "a rounding-level tick is not news"
    assert m_a != m_c, "a real reward is"
    assert m_a != m_d, "and an exit certainly is"
