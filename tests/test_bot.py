import json
import time

import httpx
import pytest

from portfolio import bot as botmod
from portfolio import config as cfgmod
from portfolio import db as dbmod

CFG = """
db_path: {db}
interval_minutes: 15
notify:
  telegram:
    enabled: true
    bot_token: TOKEN
    chat_id: "42"
addresses:
  - chain: bitcoin
    address: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa
    label: btc
    watch: [native]
tokens: []
"""


class Prices:
    """Priced like the real book, but never over the network."""

    def __init__(self, table=None):
        self.table = table or {"bitcoin:native": 100_000.0}
        self.refreshed = 0
        self.ttls: list[int | None] = []

    async def refresh(self, assets, ttl_minutes=None):
        self.refreshed += 1
        self.ttls.append(ttl_minutes)

    def usd(self, key):
        return self.table.get(key)

    def value(self, key, amount_raw, decimals):
        price = self.table.get(key)
        return None if price is None else amount_raw / 10 ** decimals * price


class Telegram:
    """Records every Bot API call and answers with a shaped response."""

    def __init__(self, updates=None, edit_status=200,
                 edit_error="message is not modified"):
        self.updates = list(updates or [])
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.acked: list[str] = []
        self.calls: list[str] = []
        self.edit_status = edit_status
        # Which 400 it is decides the answer, so a test has to be able to pick.
        self.edit_error = edit_error

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(method)
        payload = json.loads(request.content or b"{}")
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if method == "sendMessage":
            self.sent.append(payload)
        if method == "answerCallbackQuery":
            self.acked.append(payload.get("text", ""))
        if method == "editMessageText":
            if self.edit_status != 200:
                return httpx.Response(self.edit_status, text=self.edit_error)
            self.edited.append(payload)
        return httpx.Response(200, json={"ok": True, "result": True})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def update(text: str, uid: int = 1, chat: str = "42", date=None) -> dict:
    return {"update_id": uid,
            "message": {"date": date if date is not None else int(time.time()),
                        "chat": {"id": chat, "type": "private"},
                        "text": text}}


def tap(data: str, chat: str = "42", message_id: int = 1, uid: int = 2) -> dict:
    return {"update_id": uid,
            "callback_query": {"id": "cb1", "data": data,
                               "message": {"message_id": message_id,
                                           "date": int(time.time()),
                                           "chat": {"id": chat, "type": "private"}}}}


@pytest.fixture()
def setup(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(CFG.format(db=tmp_path / "c.db"))
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    dbmod.sync_config(conn, cfg)
    aid = conn.execute("SELECT id FROM addresses").fetchone()[0]
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       kind, whitelisted)
                    VALUES ('bitcoin:native','bitcoin',NULL,'BTC',8,'native',1)""")
    sid = conn.execute("SELECT id FROM assets").fetchone()[0]
    conn.execute("""INSERT INTO balances(address_id, asset_id, amount_raw, updated_at)
                    VALUES (?,?,?,?)""", (aid, sid, str(50_000_000), "2026-01-01"))
    return cfg, conn


def make(setup, tg, scan=None, prices=None):
    cfg, conn = setup
    return botmod.CommandBot(cfg, conn, prices or Prices(), scan=scan, client=tg.client())


async def test_a_message_from_another_chat_is_never_answered(setup):
    """Anyone who guesses the bot's name can write to it. A bot that answers
    them hands a stranger the contents of the portfolio."""
    tg = Telegram()
    bot = make(setup, tg)
    assert await bot.handle(update("/portfolio", chat="9999")) is None
    assert tg.sent == []


async def test_portfolio_answers_with_what_the_last_scan_saw(setup):
    """The command must read the database, not fetch: a question asked five
    times in a minute must not cost five full scans."""
    tg = Telegram()
    prices = Prices()
    bot = make(setup, tg, prices=prices)
    assert await bot.handle(update("/portfolio")) == "portfolio"
    assert prices.refreshed == 1, "prices are cheap and stale prices are wrong"
    assert "0.5" in tg.sent[0]["text"] and "BTC" in tg.sent[0]["text"]


async def test_digest_on_demand_does_not_move_the_daily_baseline(setup):
    """`digest.send` records the total the next morning measures against.
    Answering /digest at lunch with that write would shrink tomorrow's
    'change since yesterday' to nothing."""
    cfg, conn = setup
    tg = Telegram()
    bot = make(setup, tg)
    assert await bot.handle(update("/digest")) == "digest"
    assert dbmod.get_meta(conn, "digest:last_date") is None
    assert dbmod.get_meta(conn, "digest:last_total") is None


async def test_the_bot_name_suffix_used_in_groups_is_stripped(setup):
    """Telegram delivers '/portfolio@my_bot' whenever the chat is a group;
    matching the raw word would leave every command unknown there."""
    tg = Telegram()
    bot = make(setup, tg)
    assert await bot.handle(update("/portfolio@watcher_bot")) == "portfolio"


async def test_an_unknown_command_gets_the_help_instead_of_silence(setup):
    tg = Telegram()
    bot = make(setup, tg)
    assert await bot.handle(update("/moon")) is None
    assert "/portfolio" in tg.sent[0]["text"]


async def test_a_stale_command_is_not_answered(setup):
    """The watcher can be down for a day. Telegram keeps updates for 24 hours,
    so a restart would otherwise reply to yesterday's questions as if they had
    just been asked."""
    tg = Telegram()
    bot = make(setup, tg)
    assert await bot.handle(update("/portfolio", date=int(time.time()) - 86_400)) is None
    assert tg.sent == []


async def test_a_handler_that_blows_up_answers_instead_of_dying(setup):
    """A broken price source must not take the command channel down with it -
    the next command still has to work."""
    cfg, conn = setup

    class Broken(Prices):
        async def refresh(self, assets, ttl_minutes=None):
            raise RuntimeError("coingecko down")

    tg = Telegram()
    bot = make(setup, tg, prices=Broken())
    assert await bot.handle(update("/portfolio")) == "portfolio"
    assert "coingecko down" in tg.sent[0]["text"]
    assert await bot.handle(update("/status")) == "status"


async def test_scan_runs_the_watch_loops_own_tick(setup):
    """/scan must be the same serialized call the schedule makes; a second
    scanner against one database doubles every request."""
    calls = []

    class Res:
        probed, changed, new_events, confirmed, failed = 3, 1, 2, 0, []

    async def scan():
        calls.append(1)
        return Res()

    tg = Telegram()
    bot = make(setup, tg, scan=scan)
    assert await bot.handle(update("/scan")) == "scan"
    assert len(calls) == 1
    assert "probed 3" in tg.sent[0]["text"]


async def test_the_first_scan_after_a_restart_is_not_refused(setup, monkeypatch):
    """time.monotonic() counts from the start of the process in a container,
    not from the machine's boot. A "never scanned" marker of 0.0 therefore
    reads as "scanned just now" for the first minute of every restart, and the
    watcher answers a tap on Scan by asking for a scan that never happened.
    On a long-running host the clock is large enough to hide this entirely."""
    calls = []

    class Res:
        probed, changed, new_events, confirmed, failed = 1, 0, 0, 0, []

    async def scan():
        calls.append(1)
        return Res()

    monkeypatch.setattr(botmod.time, "monotonic", lambda: 5.0)  # a fresh container
    tg = Telegram()
    bot = make(setup, tg, scan=scan)
    await bot.handle(update("/scan"))
    assert len(calls) == 1, "the gap is between two scans, not since the epoch"


async def test_scan_asked_for_twice_in_a_row_only_runs_once(setup):
    """The expensive path is the whole reason probe() exists. A held-down
    button must not become a fetch storm."""
    calls = []

    class Res:
        probed = changed = new_events = confirmed = 0
        failed = []

    async def scan():
        calls.append(1)
        return Res()

    tg = Telegram()
    bot = make(setup, tg, scan=scan)
    await bot.handle(update("/scan", uid=1))
    await bot.handle(update("/scan", uid=2))
    assert len(calls) == 1
    assert "just scanned" in tg.sent[1]["text"]


async def test_scan_without_a_watch_loop_says_so(setup):
    tg = Telegram()
    bot = make(setup, tg, scan=None)
    await bot.handle(update("/scan"))
    assert "not running" in tg.sent[0]["text"]


async def test_health_names_the_source_that_went_quiet(setup):
    """Nobody reads fail_count at a terminal; by the time an address stops
    reporting, the only place anyone is looking is Telegram."""
    cfg, conn = setup
    aid = conn.execute("SELECT id FROM addresses").fetchone()[0]
    conn.execute("""INSERT INTO cursors(chain, address_id, scope, last_ok_at, fail_count)
                    VALUES ('bitcoin', ?, '', '2020-01-01T00:00:00+00:00', 7)""", (aid,))
    tg = Telegram()
    bot = make(setup, tg)
    assert await bot.handle(update("/health")) == "health"
    text = tg.sent[0]["text"]
    assert "btc" in text and "fail 7" in text


async def test_a_confirmed_update_is_not_replayed_after_a_restart(setup):
    """Telegram redelivers every unconfirmed update. Without a stored offset a
    crash loop would re-run the same /scan on every start."""
    cfg, conn = setup
    tg = Telegram([update("/status", uid=57)])
    bot = botmod.CommandBot(cfg, conn, Prices(), client=tg.client(), poll_timeout=0)
    await bot._resume()
    for upd in await bot._updates():
        bot._offset = upd["update_id"] + 1
        dbmod.set_meta(conn, botmod.OFFSET_KEY, str(bot._offset))
        await bot.handle(upd)
    assert dbmod.get_meta(conn, botmod.OFFSET_KEY) == "58"

    again = botmod.CommandBot(cfg, conn, Prices(), client=Telegram().client())
    await again._resume()
    assert again._offset == 58


async def test_a_cold_start_throws_the_backlog_away(setup):
    """Commands sent while the bot did not exist are history. Answering them
    on first start is a burst of replies to questions nobody remembers."""
    cfg, conn = setup
    tg = Telegram([update("/scan", uid=100), update("/scan", uid=101)])
    bot = botmod.CommandBot(cfg, conn, Prices(), client=tg.client())
    await bot._resume()
    assert bot._offset == 102
    assert tg.sent == [], "the backlog is skipped, not answered"


async def test_every_answer_offers_the_next_step_as_buttons(setup):
    """The commands hang under the message, not in a permanent strip above the
    phone's keyboard: six commands asked a few times a day do not earn screen
    space that never goes away."""
    tg = Telegram()
    bot = make(setup, tg)
    await bot.handle(update("/portfolio"))
    rows = tg.sent[-1]["reply_markup"]["inline_keyboard"]
    labels = [b["text"] for row in rows for b in row]
    assert labels[0].endswith("Refresh"), "the first button repeats this command"
    assert [b["callback_data"] for row in rows for b in row][0] == "cmd:portfolio"
    assert all(len(row) <= 3 for row in rows), "three buttons fit a phone's width"


async def test_a_tapped_button_rewrites_the_message_it_hangs_under(setup):
    """Telegram's own guidance: editing is faster and smoother than answering
    with a new message, and a refreshed portfolio is the same message again."""
    tg = Telegram()
    bot = make(setup, tg)
    assert await bot.handle(tap("cmd:portfolio", message_id=7)) == "portfolio"
    assert tg.calls == ["editMessageText", "answerCallbackQuery"]
    assert tg.edited[0]["message_id"] == 7
    assert tg.sent == [], "nothing new lands in the chat"


async def test_a_button_is_acknowledged_even_when_the_command_fails(setup):
    """An unanswered callback spins on the button until Telegram gives up."""

    class Broken(Prices):
        async def refresh(self, assets, ttl_minutes=None):
            raise RuntimeError("coingecko down")

    tg = Telegram()
    bot = make(setup, tg, prices=Broken())
    await bot.handle(tap("cmd:portfolio"))
    assert "answerCallbackQuery" in tg.calls
    assert "coingecko down" in tg.edited[0]["text"]


async def test_a_tap_from_another_chat_is_not_served(setup):
    tg = Telegram()
    bot = make(setup, tg)
    assert await bot.handle(tap("cmd:portfolio", chat="9999")) is None
    assert tg.edited == [] and tg.sent == []
    assert "answerCallbackQuery" in tg.calls, "the spinner still has to stop"


async def test_an_old_message_is_still_refreshable(setup):
    """Unlike a typed command, a button carries the date the bot wrote the
    message. Tapping Refresh on yesterday's portfolio is a question asked now,
    and the staleness rule must not eat it."""
    tg = Telegram()
    bot = make(setup, tg)
    query = tap("cmd:portfolio")
    query["callback_query"]["message"]["date"] = int(time.time()) - 86_400
    assert await bot.handle(query) == "portfolio"


async def test_an_answer_too_long_to_edit_arrives_as_new_messages(setup):
    """An edit replaces one message; a digest split into three cannot fit. It
    must still be delivered rather than dropped."""
    tg = Telegram()
    bot = make(setup, tg)
    bot._render.texts = lambda msg: ["a", "b", "c"]
    await bot.handle(tap("cmd:status", message_id=5))
    assert tg.edited == []
    assert [("reply_markup" in m) for m in tg.sent] == [False, False, True]


async def test_an_edit_refused_for_any_other_reason_falls_back_to_sending(setup):
    """A message too old to edit, or deleted out from under the buttons, still
    owes an answer. Treating that 400 like a bad token would kill the whole
    command channel."""
    tg = Telegram(edit_status=400, edit_error="message to edit not found")
    bot = make(setup, tg)
    assert await bot.handle(tap("cmd:status")) == "status"
    assert len(tg.sent) == 1, "the answer still arrives"


async def test_a_refresh_that_changes_nothing_leaves_the_chat_alone(setup):
    """Telegram 400s an edit whose text is identical, which is exactly what a
    tap on Refresh produces when no price moved. Sending the same numbers again
    answers "anything new?" with a duplicate - the one reply that is worse than
    silence. The button says so instead."""
    tg = Telegram(edit_status=400)          # "message is not modified"
    bot = make(setup, tg)
    assert await bot.handle(tap("cmd:status")) == "status"
    assert tg.sent == [], "no second copy of the same message"
    assert tg.acked == ["No change"], "the tap is still answered, on the button"


async def test_a_command_prices_at_the_command_ttl_not_the_loops(setup):
    """The background TTL is a quota decision - four CoinGecko calls per
    refresh, paid on a timer nobody is watching. A person asking is the other
    case: the total is on screen, so it is priced now. Sharing one TTL would
    force a choice between a stale answer and a quota bill."""
    cfg, conn = setup
    cfg.prices.ttl_minutes = 60
    cfg.prices.command_ttl_minutes = 1
    tg = Telegram()
    prices = Prices()
    bot = make(setup, tg, prices=prices)
    await bot.handle(update("/portfolio"))
    assert prices.ttls == [1], "the loop's 60 minutes must not reach a command"
