"""The value series drawn as a PNG, for a phone screen.

Pillow and nothing else: axes, ticks and labels are drawn by hand, because the
alternative is a plotting library several times the size of the rest of the
image (PLAN §7). The font is the one embedded in Pillow, so a slim container
needs no system fonts - and it has no glyph for a minus sign, an en dash or an
arrow, which is why every label here is ASCII apart from the middle dot.

Drawn at twice the size and scaled down: Pillow does not antialias lines, and a
2px line drawn at 1x is a staircase.
"""

import math
from datetime import datetime, timedelta, tzinfo
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from .history import Point

WIDTH, HEIGHT = 1080, 810
"""4:3, under the 1280px Telegram keeps a photo at without recompressing it.
Not 16:9: shown in a chat on a phone held upright, the picture is about 380pt
wide, and every pixel of height is room the line gets to move in."""

TEXT = {"title": 34, "hero": 66, "change": 34, "label": 28}
"""Pixel sizes, chosen for that 380pt: a 28px label lands near 10pt. At 20px,
the first draft's size, the axis read as 6.5pt - legible on a monitor and
nowhere else."""

SUPERSAMPLE = 2

THEMES = {
    "light": {"surface": "#fcfcfb", "primary": "#0b0b0b", "secondary": "#52514e",
              "muted": "#898781", "grid": "#e1e0d9", "series": "#2a78d6"},
    "dark": {"surface": "#1a1a19", "primary": "#f0efec", "secondary": "#c3c2b7",
             "muted": "#898781", "grid": "#2c2c2a", "series": "#3987e5"},
}

AREA_ALPHA = 26
"""The wash under the line, about 10%: a fill, never a block."""

MAX_TICKS = 6


def render(points: list[Point], theme: str = "light", tz: tzinfo | None = None,
           title: str = "Portfolio value") -> bytes:
    """PNG bytes of the series. Times are shown in `tz`, local by default.

    A point with something unpriced is left out and the line broken there: the
    total is short by whatever nobody quoted, and drawn, a provider going quiet
    reads as money lost.
    """
    if len(points) < 2:
        raise ValueError("not enough history to draw a chart")
    colors = THEMES[theme]
    priced = [p for p in points if not p.unpriced]
    if len(priced) < 2:
        raise ValueError("nothing in this period could be priced")
    s = SUPERSAMPLE
    img = Image.new("RGB", (WIDTH * s, HEIGHT * s), colors["surface"])
    draw = ImageDraw.Draw(img)

    def font(size):
        return ImageFont.load_default(size=size * s)

    first, last = priced[0], priced[-1]
    lo = min(p.usd for p in priced)
    hi = max(p.usd for p in priced)

    # Header: what it is, what it is worth now, how that moved over the period.
    left, right = 36, WIDTH - 36
    draw.text((left * s, 56 * s), title, font=font(TEXT["title"]),
              fill=colors["secondary"], anchor="ls")
    draw.text((left * s, 132 * s), f"${last.usd:,.0f}", font=font(TEXT["hero"]),
              fill=colors["primary"], anchor="ls")
    value_end = draw.textlength(f"${last.usd:,.0f}", font=font(TEXT["hero"])) / s
    change = last.usd - first.usd
    pct = change / first.usd * 100 if first.usd else 0.0
    sign = "+" if change >= 0 else "-"
    draw.text(((left + value_end + 20) * s, 132 * s),
              f"{sign}${abs(change):,.0f}  ({sign}{abs(pct):.2f}%)",
              font=font(TEXT["change"]), fill=colors["secondary"], anchor="ls")
    start_local, end_local = (p.ts.astimezone(tz) for p in (points[0], points[-1]))
    span = (_day(end_local) if start_local.date() == end_local.date()
            else f"{_day(start_local)} - {_day(end_local)}")
    gaps = sum(1 for p in points if p.unpriced)
    if gaps:
        span += f"  ·  {gaps} unpriced point{'s' if gaps > 1 else ''} left out"
    draw.text((right * s, 56 * s), span, font=font(TEXT["label"]), fill=colors["muted"],
              anchor="rs")
    draw.text((right * s, 132 * s), f"as of {end_local:%H:%M}",
              font=font(TEXT["label"]), fill=colors["muted"], anchor="rs")

    # Value axis: clean round ticks inside a padded range. Not from zero - a
    # line is read by its shape, and a $180k portfolio drawn from $0 is flat.
    pad = (hi - lo) * 0.08 or max(abs(hi) * 0.01, 1.0)
    y_lo, y_hi = lo - pad, hi + pad
    step = _nice_step((y_hi - y_lo) / 4)
    y_ticks = [t * step for t in range(math.ceil(y_lo / step), math.floor(y_hi / step) + 1)]
    labels = [_compact(t, step) for t in y_ticks]
    label_w = max(draw.textlength(text, font=font(TEXT["label"])) for text in labels) / s
    plot_l, plot_r = left + label_w + 16, right
    plot_t, plot_b = 190, HEIGHT - 72

    def y_of(usd):
        return plot_b - (usd - y_lo) / (y_hi - y_lo) * (plot_b - plot_t)

    t0, t1 = points[0].ts.timestamp(), points[-1].ts.timestamp()

    def x_of(ts):
        return plot_l + (ts.timestamp() - t0) / (t1 - t0) * (plot_r - plot_l)

    for tick, text in zip(y_ticks, labels):
        y = y_of(tick)
        draw.line([(plot_l * s, y * s), (plot_r * s, y * s)], fill=colors["grid"], width=s)
        draw.text(((plot_l - 16) * s, y * s), text, font=font(TEXT["label"]),
                  fill=colors["muted"], anchor="rm")
    for tick, text in _time_ticks(start_local, end_local):
        x = x_of(tick)
        draw.line([(x * s, plot_b * s), (x * s, (plot_b + 8) * s)], fill=colors["grid"],
                  width=s)
        draw.text((x * s, (plot_b + 16) * s), text, font=font(TEXT["label"]),
                  fill=colors["muted"], anchor="mt")

    segments, current = [], []
    for p in points:
        if p.unpriced:
            if current:
                segments.append(current)
            current = []
        else:
            current.append((x_of(p.ts) * s, y_of(p.usd) * s))
    if current:
        segments.append(current)

    wash = Image.new("RGBA", img.size, (0, 0, 0, 0))
    wash_draw = ImageDraw.Draw(wash)
    series = _rgb(colors["series"])
    for seg in segments:
        if len(seg) > 1:
            wash_draw.polygon(seg + [(seg[-1][0], plot_b * s), (seg[0][0], plot_b * s)],
                              fill=series + (AREA_ALPHA,))
    img = Image.alpha_composite(img.convert("RGBA"), wash).convert("RGB")
    draw = ImageDraw.Draw(img)
    for seg in segments:
        if len(seg) > 1:
            draw.line(seg, fill=colors["series"], width=2 * s, joint="curve")

    # The end dot, ringed in the surface so it reads on top of the line.
    x, y = x_of(last.ts) * s, y_of(last.usd) * s
    for radius, color in ((7, colors["surface"]), (5, colors["series"])):
        draw.ellipse([x - radius * s, y - radius * s, x + radius * s, y + radius * s],
                     fill=color)

    out = BytesIO()
    img.resize((WIDTH, HEIGHT), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


def _nice_step(raw: float) -> float:
    """The smallest of 1, 2, 2.5 or 5 × 10^k at least `raw`."""
    magnitude = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * magnitude >= raw:
            return m * magnitude
    return 10 * magnitude


def _compact(usd: float, step: float) -> str:
    """$180K, $182.5K, $1.25M - only as many decimals as the step needs."""
    for unit, suffix in ((1e6, "M"), (1e3, "K"), (1, "")):
        if abs(usd) >= unit or unit == 1:
            scaled_step = step / unit
            decimals = 0
            while decimals < 3 and abs(round(scaled_step, decimals) - scaled_step) > 1e-9:
                decimals += 1
            sign = "-" if usd < 0 else ""
            return f"{sign}${abs(usd) / unit:,.{decimals}f}{suffix}"
    raise AssertionError("unreachable")


def _day(t: datetime) -> str:
    return f"{t:%b} {t.day}"


def _time_ticks(start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    """Ticks on local midnights - Mondays once they are a week or more apart -
    or on round hours when the period is shorter than two days. At most
    MAX_TICKS of them."""
    span = end - start
    if span <= timedelta(days=2):
        for hours in (1, 2, 3, 6, 12):
            if span / timedelta(hours=hours) <= MAX_TICKS:
                break
        t = start.replace(minute=0, second=0, microsecond=0)
        ticks = []
        while t <= end:
            if t >= start and t.hour % hours == 0:
                ticks.append((t, _day(t) if t.hour == 0 else f"{t:%H:%M}"))
            t += timedelta(hours=1)
        return ticks
    for days in (1, 2, 3, 7, 14, 28):
        if span / timedelta(days=days) <= MAX_TICKS:
            break
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    ticks = []
    while day <= end:
        if days >= 7:
            aligned = day.weekday() == 0 and (day.toordinal() // 7) % (days // 7) == 0
        else:
            aligned = day.toordinal() % days == 0
        if day >= start and aligned:
            ticks.append((day, _day(day)))
        day += timedelta(days=1)
    return ticks


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
