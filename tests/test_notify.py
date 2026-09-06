import pytest

from portfolio.models import Block, Message
from portfolio.notify.router import Router
from portfolio.notify.telegram import TelegramNotifier
from portfolio.retry import Permanent, Unavailable, with_retry


class Fake:
    def __init__(self, name, fail=None):
        self.name = name
        self.fail = fail
        self.calls = 0

    async def send(self, msg):
        self.calls += 1
        if self.fail:
            raise self.fail


async def test_one_dead_channel_does_not_suppress_the_others():
    """A broken webhook must never swallow the alert about the money."""
    ok, dead = Fake("telegram"), Fake("discord", Permanent("bad webhook"))
    results = await Router([ok, dead]).send(Message(title="t", body="b"))
    assert results["telegram"] is None
    assert "bad webhook" in results["discord"]
    assert ok.calls == 1


async def test_permanent_errors_are_not_retried():
    n = Fake("telegram", Permanent("401 unauthorized"))
    await Router([n]).send(Message(title="t", body="b"))
    assert n.calls == 1, "bad credentials will not become good on attempt 2"


async def test_transient_errors_are_retried_then_reported():
    n = Fake("telegram", RuntimeError("503"))
    results = await Router([n]).send(Message(title="t", body="b"))
    assert n.calls == 3
    assert "503" in results["telegram"]


async def test_with_retry_returns_first_success():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("boom")
        return "ok"

    assert await with_retry(flaky, base=0.001) == "ok"
    assert len(calls) == 2


async def test_with_retry_gives_up_as_unavailable():
    async def dead():
        raise RuntimeError("boom")

    with pytest.raises(Unavailable):
        await with_retry(dead, attempts=2, base=0.001)


def test_a_table_keeps_its_alignment_inside_a_collapsed_section():
    """The digest's columns are padded with spaces, so they are a table only in
    a monospace font. Telegram stores expandable_blockquote and pre on the same
    range - dropping the pre would turn every digest back into a wall."""
    msg = Message(title="Daily digest", body="x",
                  blocks=[Block(title="ETH  $8,000", collapsed=True,
                                lines=["2.0000 ETH    main·eth  5,000",
                                       "1.0000 WSTETH main·eth  3,000"])])
    text = TelegramNotifier("t", "c").texts(msg)[0]
    assert "<blockquote expandable><pre>" in text
    # The title stays outside, or the reader cannot see what is hidden.
    assert text.index("<b>ETH") < text.index("<blockquote")


def test_prose_is_not_forced_into_a_table():
    """A header line in a pre block cannot wrap, so a long one scrolls sideways
    on a phone instead of folding."""
    msg = Message(title="Daily digest", body="x",
                  blocks=[Block(lines=["$190,052.24   +412.00"], mono=False)])
    assert "<pre>" not in TelegramNotifier("t", "c").texts(msg)[0]


def test_html_in_a_symbol_cannot_break_the_message():
    """Token symbols come from chain data and are attacker-controlled; one
    unescaped < makes Telegram reject the whole digest."""
    msg = Message(title="Daily digest", body="x",
                  blocks=[Block(lines=["1.0 <b>OOPS</b> main·eth 1"])])
    text = TelegramNotifier("t", "c").texts(msg)[0]
    assert "&lt;b&gt;OOPS" in text


def test_an_oversized_digest_arrives_split_rather_than_rejected():
    """Telegram refuses anything past 4096 characters. A summary nobody
    receives is worse than one that takes two messages."""
    blocks = [Block(title=f"g{i}", lines=[f"row {j} " + "x" * 30 for j in range(20)])
              for i in range(10)]
    texts = TelegramNotifier("t", "c").texts(Message(title="Daily digest", body="x",
                                                     blocks=blocks))
    assert len(texts) > 1
    assert all(len(t) <= 4096 for t in texts)
    assert sum(t.count("row 19") for t in texts) == 10, "nothing dropped in the split"


def test_a_single_huge_block_is_cut_instead_of_lost():
    """One group can outgrow a whole message on its own - a wallet with two
    hundred tokens. Sending it whole means Telegram rejects it and the day has
    no digest at all."""
    block = Block(title="ETH", lines=[f"row {i} " + "y" * 40 for i in range(200)])
    texts = TelegramNotifier("t", "c").texts(Message(title="d", body="x", blocks=[block]))
    assert len(texts) > 1
    assert all(len(t) <= 4096 for t in texts)
    assert sum(t.count("row 199") for t in texts) == 1


def test_a_long_body_without_blocks_is_split_like_everything_else():
    """pipeline.render() builds a Message from `body` alone - no blocks - and a
    backlog of pending events pushes it past Telegram's 4096 characters. The
    splitter used to be reachable only through blocks, so that message was sent
    whole, refused with a 400, read as Permanent, and left every row pending:
    the next tick then sent the same thing, longer. Alerts stopped for good."""
    body = "\n".join(f"wallet-{i}  → 0.5 BTC ($42,000.00)  bc1qar0srrr…" for i in range(80))
    texts = TelegramNotifier("t", "c").texts(Message(title="Portfolio: 80 changes",
                                                     body=body))
    assert len(texts) > 1, "a message this long has to arrive in pieces"
    assert all(len(t) <= 4096 for t in texts)
    assert "wallet-0 " in texts[0] and "wallet-79 " in texts[-1], "nothing dropped"


def test_a_short_body_still_arrives_as_exactly_one_message():
    """Splitting must not cost the common case an extra message."""
    texts = TelegramNotifier("t", "c").texts(Message(title="Portfolio: 1 change",
                                                     body="npg → 0.5 BTC"))
    assert len(texts) == 1
    assert "npg → 0.5 BTC" in texts[0]


def test_a_body_keeps_being_escaped_when_it_is_split():
    """HTML parse mode: one unescaped < rejects the whole message, and the
    escaping used to live on the path that is now gone."""
    texts = TelegramNotifier("t", "c").texts(Message(title="t", body="a <b> & c"))
    assert "&lt;b&gt; &amp; c" in texts[0]
