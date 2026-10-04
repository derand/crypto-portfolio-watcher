import httpx
import pytest

from portfolio.chains.base import Target
from portfolio.protocols.beacon import BeaconSource
from portfolio.retry import Unavailable

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


async def test_a_validator_the_answer_leaves_out_costs_the_tick():
    """An exit is a status and the row stays; a validator simply absent from
    the answer is a short answer. Read as data it closes the position - a
    terminal zero in the history and a "closed validator" alert over 32 ETH
    that never moved."""
    s, _ = source([validator(1)], validators={ME: [1, 2]})
    with pytest.raises(Unavailable) as e:
        await s.fetch(TARGET)
    assert "2" in str(e.value)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def ticking(balances, start=0.0, step=900.0):
    """A source answering each balance in turn, fifteen minutes apart."""
    rows = iter(balances)
    clock = Clock()
    clock.now = start - step

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now += step
        return httpx.Response(200, json={"data": [validator(balance_gwei=next(rows))]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BeaconSource({ME: [999999]}, url="http://beacon", client=client, clock=clock)


async def trends(balances):
    s = ticking(balances)
    out = []
    for _ in balances:
        positions, _ = await s.fetch(TARGET)
        out.append(positions[0].extra.get("trend"))
    return out


BASE = 32_000_000_000
EARN = 19_000          # gwei a validator gains in fifteen minutes


async def test_no_trend_until_an_hour_has_been_seen():
    """A trend read off one tick is noise: a single missed attestation is a
    small loss. An hour is ~9 attestations, and losing most of them is what
    offline looks like."""
    got = await trends([BASE + i * EARN for i in range(6)])
    assert got == [None, None, None, None, "up", "up"]


async def test_a_falling_balance_reads_down():
    falling = [BASE + 100_000 - i * 15_000 for i in range(6)]
    got = await trends(falling)
    assert got[4:] == ["down", "down"]


async def test_one_missed_attestation_does_not_read_down():
    """A working validator misses an attestation now and then; the hour's
    earnings cover it."""
    bal = [BASE, BASE + EARN, BASE + 2 * EARN - 30_000, BASE + 3 * EARN - 30_000,
           BASE + 4 * EARN - 30_000]
    assert (await trends(bal))[4] == "up"


async def test_the_withdrawal_sweep_is_not_a_loss():
    """Every week or so the excess over 32 ETH leaves for the withdrawal
    address. Read as a loss, every sweep would page "offline" about a
    validator that never missed a thing."""
    high = BASE + 14_000_000
    bal = [high, high + EARN, high + 2 * EARN, high + 3 * EARN, high + 4 * EARN,
           BASE + 100, BASE + 100 + EARN]
    got = await trends(bal)
    assert got[4] == "up"
    assert "down" not in got, got


async def test_a_slashing_is_a_loss_not_a_sweep():
    """It is a large drop too, but it lands below the effective balance."""
    high = BASE + 14_000_000
    bal = [high + i * EARN for i in range(5)] + [BASE - 1_000_000_000] * 5
    got = await trends(bal)
    assert got[-1] == "down"


async def test_the_trend_moves_the_marker():
    """An offline validator loses a milli-ETH only every half a day. Without
    the trend in the marker the pipeline would not hear of it for that long."""
    s = ticking([BASE + 500_000 - i * 15_000 for i in range(5)])
    markers = []
    for _ in range(5):
        _, m = await s.fetch(TARGET)
        markers.append(m)
    assert markers[3] != markers[4]
