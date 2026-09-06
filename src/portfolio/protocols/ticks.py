"""Uniswap v3 tick math, in integers.

A v3 position stores a liquidity coefficient and two tick bounds. It does not
store how much of anything it holds: that depends on where the pool's price
currently sits inside those bounds, so the amounts have to be computed. This is
the arithmetic that does it - a direct port of Uniswap's TickMath and
LiquidityAmounts.

Integers throughout, as everywhere else in this codebase. The temptation here is
real, because the formulas are written with square roots and look like floating
point: they are not. The chain gives `sqrtPriceX96` already rooted, and the tick
bounds are converted by table lookup and shifts, so nothing is ever approximated
except by the deliberate truncation the protocol itself performs.
"""

Q96 = 1 << 96
MIN_TICK = -887272
MAX_TICK = 887272

# TickMath.getSqrtRatioAtTick: 1.0001^(tick/2) built from powers of two.
# Each constant is 1.0001^(2^i / 2) in Q128.128, so multiplying the ones
# selected by the bits of |tick| composes the whole exponent with no loss
# beyond the shift. Transcribed from the Solidity original and checked against
# live pools - see tests: for a pool reporting tick T and sqrtPriceX96 P, the
# bracket ratio(T) <= P < ratio(T+1) must hold, which a wrong digit breaks.
_FACTORS = (
    (0x1, 0xfffcb933bd6fad37aa2d162d1a594001),
    (0x2, 0xfff97272373d413259a46990580e213a),
    (0x4, 0xfff2e50f5f656932ef12357cf3c7fdcc),
    (0x8, 0xffe5caca7e10e4e61c3624eaa0941cd0),
    (0x10, 0xffcb9843d60f6159c9db58835c926644),
    (0x20, 0xff973b41fa98c081472e6896dfb254c0),
    (0x40, 0xff2ea16466c96a3843ec78b326b52861),
    (0x80, 0xfe5dee046a99a2a811c461f1969c3053),
    (0x100, 0xfcbe86c7900a88aedcffc83b479aa3a4),
    (0x200, 0xf987a7253ac413176f2b074cf7815e54),
    (0x400, 0xf3392b0822b70005940c7a398e4b70f3),
    (0x800, 0xe7159475a2c29b7443b29c7fa6e889d9),
    (0x1000, 0xd097f3bdfd2022b8845ad8f792aa5825),
    (0x2000, 0xa9f746462d870fdf8a65dc1f90e061e5),
    (0x4000, 0x70d869a156d2a1b890bb3df62baf32f7),
    (0x8000, 0x31be135f97d08fd981231505542fcfa6),
    (0x10000, 0x9aa508b5b7a84e1c677de54f3e99bc9),
    (0x20000, 0x5d6af8dedb81196699c329225ee604),
    (0x40000, 0x2216e584f5fa1ea926041bedfe98),
    (0x80000, 0x48a170391f7dc42444e8fa2),
)
_MAX_UINT256 = (1 << 256) - 1


def sqrt_ratio_at_tick(tick: int) -> int:
    """The square root of the price at `tick`, in Q64.96 - the same units the
    pool reports its current price in, so the two can be compared directly."""
    if not MIN_TICK <= tick <= MAX_TICK:
        raise ValueError(f"tick {tick} is outside the representable range")
    abs_tick = abs(tick)
    ratio = 1 << 128
    for bit, factor in _FACTORS:
        if abs_tick & bit:
            ratio = (ratio * factor) >> 128
    if tick > 0:
        ratio = _MAX_UINT256 // ratio
    # Q128.128 to Q64.96, rounding up, exactly as the original does.
    return (ratio >> 32) + (1 if ratio % (1 << 32) else 0)


def amounts_for_liquidity(sqrt_price: int, tick_lower: int, tick_upper: int,
                          liquidity: int) -> tuple[int, int]:
    """How much of each token a position holds right now.

    Three cases, and the two outer ones are the whole reason a range position
    needs watching: below its range it is entirely token0, above it entirely
    token1. Either way it has stopped earning fees, and the swing between them
    is what an "out of range" alert is about.
    """
    lower = sqrt_ratio_at_tick(tick_lower)
    upper = sqrt_ratio_at_tick(tick_upper)
    if lower > upper:
        lower, upper = upper, lower
    if liquidity == 0:
        return 0, 0

    if sqrt_price <= lower:
        return _amount0(lower, upper, liquidity), 0
    if sqrt_price >= upper:
        return 0, _amount1(lower, upper, liquidity)
    return (_amount0(sqrt_price, upper, liquidity),
            _amount1(lower, sqrt_price, liquidity))


def _amount0(lower: int, upper: int, liquidity: int) -> int:
    """L * (√b - √a) / (√a * √b), kept whole by multiplying before dividing."""
    return ((liquidity << 96) * (upper - lower) // upper) // lower


def _amount1(lower: int, upper: int, liquidity: int) -> int:
    """L * (√b - √a), shifted back out of Q96."""
    return liquidity * (upper - lower) // Q96


def in_range(tick: int, tick_lower: int, tick_upper: int) -> bool:
    """Uniswap's own convention: the upper bound is exclusive, so a position
    whose range ends exactly at the current tick is already out and earning
    nothing."""
    return tick_lower <= tick < tick_upper
