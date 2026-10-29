from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from PIL import Image

from portfolio import chart
from portfolio.history import Point

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def series(values, unpriced=()):
    return [Point(T0 + timedelta(hours=i), usd, 1 if i in unpriced else 0)
            for i, usd in enumerate(values)]


def image(png: bytes) -> Image.Image:
    return Image.open(BytesIO(png)).convert("RGB")


def test_the_picture_is_a_png_of_the_size_telegram_keeps():
    """Over 1280px Telegram recompresses the photo, and the hairlines go first."""
    img = image(chart.render(series([100.0, 105.0, 103.0]), tz=timezone.utc))
    assert img.size == (chart.WIDTH, chart.HEIGHT)
    assert max(img.size) <= 1280


def test_the_dark_theme_is_drawn_on_the_dark_surface():
    """A light picture in a dark chat is a flashlight at night."""
    img = image(chart.render(series([100.0, 105.0]), theme="dark", tz=timezone.utc))
    assert img.getpixel((2, chart.HEIGHT - 2)) == chart._rgb(chart.THEMES["dark"]["surface"])


def test_an_unpriced_point_breaks_the_line_instead_of_drawing_a_crash():
    """With something unquoted, the total is short by it. Drawn, a provider going
    quiet for an hour reads as a third of the portfolio lost and recovered."""
    values = [180_000.0, 181_000.0, 60_000.0, 182_000.0, 181_500.0]
    img = image(chart.render(series(values, unpriced={2}), tz=timezone.utc))
    line = chart._rgb(chart.THEMES["light"]["series"])
    # The plot spans the first point to the last, so the unpriced one sits in
    # the middle column - which must hold no line at all, low or otherwise.
    x = (chart.WIDTH + 100) // 2
    column = [y for y in range(200, chart.HEIGHT - 80)
              if all(abs(a - b) < 40 for a, b in zip(img.getpixel((x, y)), line))]
    assert column == []


def test_a_series_too_short_to_draw_says_so():
    """The bot turns this into an answer; a blank picture would answer nothing."""
    with pytest.raises(ValueError):
        chart.render(series([100.0]))
    with pytest.raises(ValueError):
        chart.render(series([100.0, 101.0], unpriced={0, 1}))


@pytest.mark.parametrize("value, step, text", [
    (180_000, 10_000, "$180K"),
    (182_500, 2_500, "$182.5K"),
    (1_250_000, 250_000, "$1.25M"),
    (750, 250, "$750"),
])
def test_axis_labels_carry_only_the_decimals_the_step_needs(value, step, text):
    """$182.50K beside $180.00K is noise; $182K beside $182K is two ticks with
    one name."""
    assert chart._compact(value, step) == text


def test_value_ticks_are_round():
    assert chart._nice_step(4_100) == 5_000
    assert chart._nice_step(2_100) == 2_500
    assert chart._nice_step(900) == 1_000


def test_weekly_ticks_land_on_mondays():
    """Ticks counted from the first point move with every request, so the same
    week is labelled Thursday today and Friday tomorrow."""
    start = datetime(2026, 9, 10, 3, tzinfo=timezone.utc)
    ticks = chart._time_ticks(start, start + timedelta(days=49))
    assert ticks and all(t.weekday() == 0 for t, _ in ticks)
    assert len(ticks) <= chart.MAX_TICKS


def test_a_day_is_labelled_by_the_hour():
    start = datetime(2026, 10, 29, 0, 30, tzinfo=timezone.utc)
    ticks = chart._time_ticks(start, start + timedelta(hours=22))
    assert [text for _, text in ticks] == ["06:00", "12:00", "18:00"]
