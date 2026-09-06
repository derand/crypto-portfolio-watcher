"""Command line entry point.

Phase 0 commands: config-check, init-db, status, test-notify.
scan/watch arrive with the first chain adapter in phase 1.
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from . import bot as botmod
from . import config as cfgmod
from . import db as dbmod
from . import digest
from . import discover
from .chains.evm import EvmAdapter
from .prices.sources import PriceSources
from .prices import build_prices
from .models import EventKind, Message, Severity
from .notify import Router, build_notifiers
from .retry import Permanent


def setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)-24s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx logs every request at INFO; useful with -v, noise otherwise.
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)


def cmd_config_check(args) -> int:
    cfg = cfgmod.load(args.config, args.env)
    enabled = [a for a in cfg.addresses if a.enabled]
    print(f"config      {args.config}")
    print(f"interval    {cfg.interval_minutes} min")
    print(f"db          {cfg.db_path}")
    print(f"threshold   ${cfg.thresholds.notify_usd:g} to alert")
    print(f"channels    {', '.join(cfg.notify.enabled_channels()) or '(none enabled)'}")
    from .chains import missing_sources
    for problem in missing_sources(cfg):
        print(f"  ! not watching {problem}")
    print(f"addresses   {len(enabled)} enabled / {len(cfg.addresses)} total")
    for a in cfg.addresses:
        mark = " " if a.enabled else "-"
        nets = f" [{', '.join(a.chains)}]" if a.chains else ""
        print(f"  {mark} {a.label:<12} {a.chain:<8} {a.address[:10]}…  "
              f"watch: {', '.join(a.watch)}{nets}")
    print(f"tokens      {len(cfg.tokens)} whitelisted")
    for t in cfg.tokens:
        print(f"    {t.symbol:<10} {t.chain:<10} {t.contract[:10]}…")
    return 0


def cmd_init_db(args) -> int:
    cfg = cfgmod.load(args.config, args.env)
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    n_addr, n_tok = dbmod.sync_config(conn, cfg)
    print(f"schema v{dbmod.SCHEMA_VERSION} at {cfg.db_path}")
    print(f"synced {n_addr} addresses, {n_tok} tokens from config")
    return 0


def cmd_status(args) -> int:
    cfg = cfgmod.load(args.config, args.env)
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    print(f"db          {cfg.db_path}")
    print(f"addresses   {q('SELECT COUNT(*) FROM addresses WHERE enabled=1')} enabled")
    print(f"assets      {q('SELECT COUNT(*) FROM assets')}")
    print(f"events      {q('SELECT COUNT(*) FROM events')}")
    pending = q("SELECT COUNT(*) FROM notifications WHERE status='pending'")
    failed = q("SELECT COUNT(*) FROM notifications WHERE status='failed'")
    print(f"notify      {pending} pending, {failed} failed")
    last = conn.execute("SELECT ts, kind, detail FROM events ORDER BY ts DESC LIMIT 5").fetchall()
    if last:
        print("recent")
        for r in last:
            print(f"    {r['ts'][:19]}  {r['kind']:<16} {r['detail'] or ''}")
    else:
        print("recent      (no events yet)")
    return 0


async def _run_scan(cfg, conn, router, adapters, sources, prices=None):
    from . import pipeline
    return await pipeline.scan_once(cfg, conn, router, adapters, sources, prices)


def _open(args):
    cfg = cfgmod.load(args.config, args.env)
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    dbmod.sync_config(conn, cfg)
    return cfg, conn


def cmd_scan(args) -> int:
    from .chains import build_adapters, missing_sources
    from .protocols import build_sources

    cfg, conn = _open(args)
    for problem in missing_sources(cfg):
        logging.getLogger("portfolio").error("not watching %s", problem)
    adapters = build_adapters(cfg)
    sources = build_sources(cfg)
    prices = build_prices(cfg, conn)
    router = Router(build_notifiers(cfg))

    async def go():
        try:
            return await _run_scan(cfg, conn, router, adapters, sources, prices)
        finally:
            for a in (*adapters.values(), *sources.values(), prices):
                if hasattr(a, "aclose"):
                    await a.aclose()

    res = asyncio.run(go())
    quiet = f", {res.below_threshold} below threshold" if res.below_threshold else ""
    print(f"probed {res.probed}, changed {res.changed}, "
          f"new {res.new_events}, confirmed {res.confirmed}{quiet}")
    for f in res.failed:
        print(f"  failed: {f}", file=sys.stderr)
    for ch, err in res.delivered.items():
        print(f"  {ch}: {'sent' if err is None else 'FAILED - ' + err}")
    return 1 if res.failed else 0


def _runtime(args):
    """Everything a long-running command needs, built once and shared."""
    from .chains import build_adapters, missing_sources
    from .protocols import build_sources

    cfg, conn = _open(args)
    for problem in missing_sources(cfg):
        logging.getLogger("portfolio").error("not watching %s", problem)
    adapters = build_adapters(cfg)
    sources = build_sources(cfg)
    prices = build_prices(cfg, conn)
    router = Router(build_notifiers(cfg))
    return cfg, conn, adapters, sources, prices, router


async def _close(adapters, sources, prices) -> None:
    for a in (*adapters.values(), *sources.values(), prices):
        if hasattr(a, "aclose"):
            await a.aclose()


def _build_bot(cfg, conn, prices, scan):
    """The command bot, or None when this deployment does not answer commands."""
    if not (cfg.notify.telegram.enabled and cfg.notify.telegram.commands):
        return None
    from .bot import CommandBot
    return CommandBot(cfg, conn, prices, scan=scan)


def cmd_watch(args) -> int:
    """Loop forever at the configured interval, answering commands meanwhile.

    Nothing here is stateful: every tick is the same scan, so killing and
    restarting the process is always safe.
    """
    cfg, conn, adapters, sources, prices, router = _runtime(args)
    every = cfg.interval_minutes * 60
    lock = asyncio.Lock()

    async def tick():
        """One scan, serialized: a /scan asked for at 09:14 must not run
        alongside the 09:15 one and fetch everything twice."""
        async with lock:
            res = await _run_scan(cfg, conn, router, adapters, sources, prices)
            dbmod.set_meta(conn, botmod.LAST_TICK, datetime.now(timezone.utc).isoformat())
            return res

    async def ticking():
        log = logging.getLogger("portfolio.watch")
        log.info("watching %d addresses every %d min",
                 len([a for a in cfg.addresses if a.enabled]), cfg.interval_minutes)
        while True:
            try:
                res = await tick()
                log.info("tick: probed %d, changed %d, new %d, confirmed %d",
                         res.probed, res.changed, res.new_events, res.confirmed)
                if digest.due(cfg, conn):
                    _, results = await digest.send(cfg, conn, router, prices)
                    if any(results.values()):
                        # Not recorded, so due() stays true and the next tick
                        # tries again rather than losing the day silently.
                        log.error("daily digest failed: %s", results)
                    else:
                        log.info("daily digest sent")
            except Exception:
                # A tick must never kill the loop; the next one retries.
                log.exception("tick failed")
            await asyncio.sleep(every)

    async def go():
        bot = _build_bot(cfg, conn, prices, tick)
        try:
            # The bot is a plain second task: it never raises out of run(), so a
            # dead Telegram cannot stop the watching.
            await asyncio.gather(*([ticking()] + ([bot.run()] if bot else [])))
        finally:
            if bot:
                await bot.aclose()
            await _close(adapters, sources, prices)

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        print("stopped")
    return 0


def cmd_bot(args) -> int:
    """Answer commands without the schedule. For debugging the bot alone.

    Running this next to a `watch` on the same token means both ask Telegram for
    updates and each gets about half of them, so use one or the other.
    """
    cfg, conn, adapters, sources, prices, router = _runtime(args)
    if not cfg.notify.telegram.enabled:
        print("error: telegram is not enabled in config", file=sys.stderr)
        return 2
    lock = asyncio.Lock()

    async def tick():
        async with lock:
            res = await _run_scan(cfg, conn, router, adapters, sources, prices)
            dbmod.set_meta(conn, botmod.LAST_TICK, datetime.now(timezone.utc).isoformat())
            return res

    async def go():
        from .bot import CommandBot
        bot = CommandBot(cfg, conn, prices, scan=tick)
        try:
            await bot.run()
        finally:
            await bot.aclose()
            await _close(adapters, sources, prices)

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        print("stopped")
    return 0


def cmd_digest(args) -> int:
    cfg, conn = _open(args)
    prices = build_prices(cfg, conn)
    router = Router(build_notifiers(cfg))

    async def go():
        try:
            from . import pipeline
            await prices.refresh(pipeline.priceable_assets(conn))
            if args.dry_run:
                data = digest.collect(conn, prices)
                return digest.render(conn, data, cfg), {}
            return await digest.send(cfg, conn, router, prices)
        finally:
            await prices.aclose()

    message, results = asyncio.run(go())
    print(message.body)
    if args.dry_run:
        print("\n(dry run: nothing sent, nothing recorded)")
        return 0
    failed = {ch: err for ch, err in results.items() if err}
    if failed:
        # Nothing was recorded either, so a rerun is a real retry.
        for channel, err in failed.items():
            print(f"{channel}: FAILED - {err}", file=sys.stderr)
        return 1
    return 0


def cmd_portfolio(args) -> int:
    """State as of the last scan, valued at today's prices.

    Deliberately does not fetch balances: a scan is the expensive path and is
    already on a schedule. Prices are cheap and stale prices make the number
    wrong, so those are refreshed.
    """
    cfg, conn = _open(args)
    prices = build_prices(cfg, conn)
    router = Router(build_notifiers(cfg)) if args.send else None

    async def go():
        try:
            from . import pipeline
            await prices.refresh(pipeline.priceable_assets(conn))
            message = digest.render_state(conn, digest.collect(conn, prices), cfg)
            if router is not None:
                results = await router.send(message)
                for channel, err in results.items():
                    print(f"{channel}: {err or 'sent'}", file=sys.stderr)
            return message
        finally:
            await prices.aclose()

    print(asyncio.run(go()).body)
    return 0


def cmd_discover_tokens(args) -> int:
    """Propose whitelist candidates. Read-only: never writes the config."""
    cfg = cfgmod.load(args.config, args.env)
    if not cfg.api_keys.alchemy:
        raise Permanent("ALCHEMY_API_KEY is not set; discovery needs it")
    if not cfg.api_keys.coingecko:
        # Without a key CoinGecko allows one contract address per request, so
        # every batch 400s and the run reports nothing held. Say so up front
        # rather than let it look like a clean, empty result.
        print("warning: COINGECKO_DEMO_KEY is not set. The public tier accepts "
              "one contract per request,\n         so pricing will fail and "
              "nothing will be proposed. Get a free demo key first.\n")

    adapter = EvmAdapter(cfg.api_keys.alchemy, cfg.tokens_by_chain())
    sources = PriceSources(api_key=cfg.api_keys.coingecko)

    async def go():
        try:
            return await discover.collect(cfg, adapter, sources, args.min_usd,
                                          include_unpriced=args.show_unpriced)
        finally:
            await adapter.aclose()
            await sources.aclose()

    rows, unpriced = asyncio.run(go())
    print(discover.render(rows, args.min_usd, unpriced))
    return 0


def cmd_discover_protocols(args) -> int:
    """Propose positions the token index cannot see. Read-only, like its twin.

    No price source: what this finds is a receipt token, and a receipt token is
    listed nowhere by its own address, so there is nothing to rank by. The
    filter here is the catalog rather than a CoinGecko listing - a contract
    reached through a market's own enumeration cannot be an airdrop.
    """
    cfg = cfgmod.load(args.config, args.env)
    if not cfg.api_keys.alchemy:
        raise Permanent("ALCHEMY_API_KEY is not set; discovery needs it")

    adapter = EvmAdapter(cfg.api_keys.alchemy, cfg.tokens_by_chain())

    async def go():
        try:
            return await discover.collect_protocols(cfg, adapter)
        finally:
            await adapter.aclose()

    print(discover.render_protocols(asyncio.run(go())))
    return 0


def cmd_catalog_check(args) -> int:
    """Ask every catalog entry to enumerate itself, and report what answered.

    A catalog address does not fail loudly when it goes stale - the market
    simply lists nothing and the positions behind it read as absent. This is
    the command that turns that silence into a line of output.
    """
    from . import catalog

    cfg = cfgmod.load(args.config, args.env)
    if not cfg.api_keys.alchemy:
        raise Permanent("ALCHEMY_API_KEY is not set; the catalog is checked on chain")
    from .chains.evm import NETWORKS

    entries = catalog.load()
    adapter = EvmAdapter(cfg.api_keys.alchemy)
    # config.py allows more EVM networks than the adapter can reach, so the
    # catalog may name one there is no provider for. That is a gap to report,
    # not a crash on the first row.
    reachable = {c: e for c, e in catalog.by_chain(entries).items() if c in NETWORKS}
    unreachable = {e.chain for e in entries} - set(reachable)

    async def go():
        try:
            return {chain: await discover._receipts(adapter, chain, on_chain)
                    for chain, on_chain in reachable.items()}
        finally:
            await adapter.aclose()

    found = asyncio.run(go())
    bad = 0
    print(f"{len(entries)} entries, "
          f"{len({e.protocol for e in entries})} protocols, "
          f"{len(found)} networks checked\n")
    for entry in entries:
        if entry.chain in unreachable:
            print(f"  skip  {entry.label:26} no provider for {entry.chain}")
            continue
        tokens = found.get(entry.chain, {}).get(entry.label)
        if tokens:
            print(f"  ok    {entry.label:26} {len(tokens):3} receipt tokens")
        else:
            bad += 1
            print(f"  STALE {entry.label:26} listed nothing - check {entry.address}")
    if bad:
        print(f"\n{bad} entr{'y' if bad == 1 else 'ies'} answered nothing. An entry "
              f"that lists no tokens hides every position behind it.")
    return 1 if bad else 0


def cmd_telegram_chat_id(args) -> int:
    """Print chat ids the bot can currently see.

    Reads TELEGRAM_BOT_TOKEN straight from the environment rather than through
    Config: the config refuses to load without a chat_id, which is precisely
    what this command exists to find.
    """
    import os

    import httpx

    cfgmod.load_dotenv(Path(args.env))
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("error: TELEGRAM_BOT_TOKEN is not set in .env or the environment",
              file=sys.stderr)
        return 2

    r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15.0)
    if r.status_code != 200:
        print(f"error: telegram {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return 2

    seen = {}
    for upd in r.json().get("result", []):
        for key in ("message", "channel_post", "edited_message"):
            chat = (upd.get(key) or {}).get("chat")
            if chat:
                seen[chat["id"]] = chat

    if not seen:
        print("Bot sees no messages yet.\n"
              "  1. open the chat with your bot in Telegram\n"
              "  2. send it any message (a bot cannot start a conversation itself)\n"
              "  3. run this again\n"
              "Note: getUpdates only keeps the last 24 hours.")
        return 1

    print("chat ids the bot can see:\n")
    for cid, chat in seen.items():
        who = chat.get("title") or " ".join(
            filter(None, [chat.get("first_name"), chat.get("last_name")])) or "?"
        handle = f"@{chat['username']}" if chat.get("username") else ""
        print(f"  TELEGRAM_CHAT_ID={cid}    {chat.get('type'):<10} {who} {handle}")
    print("\nPut the one you want into .env")
    return 0


def cmd_test_notify(args) -> int:
    cfg = cfgmod.load(args.config, args.env)
    router = Router(build_notifiers(cfg))
    if not router.channels:
        print("no channels enabled in config - nothing to test", file=sys.stderr)
        return 1
    msg = Message(
        title="Portfolio watcher",
        body=(f"Self-test from phase 0.\n"
              f"Watching {len([a for a in cfg.addresses if a.enabled])} addresses, "
              f"interval {cfg.interval_minutes} min."),
        severity=Severity.NORMAL,
        kind=EventKind.SERVICE,
    )
    results = asyncio.run(router.send(msg))
    ok = True
    for channel, err in results.items():
        if err:
            ok = False
            print(f"{channel}: FAILED - {err}", file=sys.stderr)
        else:
            print(f"{channel}: sent")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="portfolio", description="Crypto portfolio watcher")
    p.add_argument("-c", "--config", default="config/portfolio.yaml")
    p.add_argument("-e", "--env", default=".env")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config-check", help="load and validate config, print what it means")
    sub.add_parser("init-db", help="create schema and sync addresses/tokens from config")
    sub.add_parser("status", help="what the database currently holds")
    sub.add_parser("scan", help="run one tick: probe, record changes, notify")
    sub.add_parser("watch", help="run scan forever at the configured interval")
    sub.add_parser("bot", help="answer Telegram commands only, without the scan loop")
    p_portfolio = sub.add_parser(
        "portfolio", help="what is held right now, at today's prices")
    p_portfolio.add_argument("--send", action="store_true",
                             help="also deliver it to the notification channels")
    p_digest = sub.add_parser("digest", help="portfolio summary; sent unless --dry-run")
    p_digest.add_argument("--dry-run", action="store_true",
                          help="print it without sending or recording the total")
    p_disc = sub.add_parser("discover-tokens",
                            help="propose whitelist candidates; prints YAML, writes nothing")
    p_disc.add_argument("--min-usd", type=float, default=10.0,
                        help="ignore holdings worth less than this (default: 10)")
    p_disc.add_argument("--show-unpriced", action="store_true",
                        help="also list holdings nothing can price; costs a metadata "
                             "call per held token, and most of them are airdrops")
    sub.add_parser("discover-protocols",
                   help="propose positions the token index cannot see; prints YAML")
    sub.add_parser("catalog-check",
                   help="ask every catalog entry to enumerate itself on chain")
    sub.add_parser("test-notify", help="send a message through every enabled channel")
    sub.add_parser("telegram-chat-id", help="find your TELEGRAM_CHAT_ID (needs the token only)")

    args = p.parse_args(argv)
    setup_logging(args.verbose)
    handlers = {
        "config-check": cmd_config_check,
        "discover-tokens": cmd_discover_tokens,
        "discover-protocols": cmd_discover_protocols,
        "catalog-check": cmd_catalog_check,
        "init-db": cmd_init_db,
        "status": cmd_status,
        "scan": cmd_scan,
        "watch": cmd_watch,
        "bot": cmd_bot,
        "digest": cmd_digest,
        "portfolio": cmd_portfolio,
        "test-notify": cmd_test_notify,
        "telegram-chat-id": cmd_telegram_chat_id,
    }
    try:
        return handlers[args.cmd](args)
    except Permanent as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ValidationError as e:
        # Pydantic's default rendering buries the message in type/url noise.
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "config"
            print(f"error: {loc}: {err['msg'].removeprefix('Value error, ')}",
                  file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
