# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A read-only crypto portfolio watcher: it polls addresses, records what changed, and
notifies. No private keys, no signing, ever. `docs/PLAN.md` is the design record and
carries the *why* behind most decisions here — read it before proposing architecture
changes, and update it when a decision turns out wrong (several already have).

## Commands

Everything runs through `./pw`, which installs nothing: it runs the CLI on the first
interpreter it finds, `$PW_PYTHON`, then `./.venv`, then `../venv` (a venv shared with
sibling projects), then `python3`. Adding a dependency is a decision to raise with the
user, not a step.

```bash
./pw config-check            # validate config, print what would be polled
./pw init-db                 # create schema, sync addresses/tokens from YAML
./pw scan                    # one tick
./pw watch                   # scan on a loop
./pw portfolio               # what is held now, at today's prices (--send to deliver)
./pw digest --dry-run        # daily summary without sending or recording
./pw discover-tokens         # propose whitelist candidates (--show-unpriced, --min-usd)
./pw discover-protocols      # propose positions the token index cannot see
./pw catalog-check           # verify every catalog entry still enumerates
./pw bot                     # answer Telegram commands only, no scan loop
./pw -v scan                 # verbose; the flag goes BEFORE the subcommand

# pytest is not a `./pw` subcommand: call whichever venv ./pw picks, directly
# (`.venv/bin/python` here, `../venv/bin/python` where the venv is shared).
.venv/bin/python -m pytest -q                      # all tests
.venv/bin/python -m pytest -q tests/test_evm.py    # one file
.venv/bin/python -m pytest -q -k marker            # one test by name
```

`init-db` only syncs config into the database; it never fetches. **After editing
`config/portfolio.yaml` you need `init-db` *and* `scan`** — otherwise the digest keeps
showing the old picture and the new address looks broken. This has caused confusion twice.

`portfolio` and `digest` both read balances from SQLite (as of the last `scan`) and refresh
prices over the network, so the dollar total moves without a scan while quantities do not.
They answer different questions and are deliberately not the same message: `portfolio` is
the full breakdown, asked for whenever you want it; `digest` is the once-a-day total,
instrument summary and what happened in 24h. Detail added to the digest is detail nobody
reads at 9am.

Under git since 2026-09-05 (PLAN §12 records the reversal). The repository must never hold
addresses or keys: `.env`, `config/portfolio.yaml`, `data/` and `docs/` are ignored,
and test fixtures use made-up addresses and validator indexes on purpose — a real one committed
to a test is a leak that looks like nothing. Commit only when asked.

**Nor which protocols are held** (PLAN §14). Aave says nothing; four niche protocols named
together identify the owner as well as an address would. So documentation names the
*canonical example of a mechanism*, never the one this portfolio happens to hold: a rate
that takes an amount is `convertToAssets(uint256)`, not whichever vault the code was written
against. The same goes for fixtures — the whitelist's real contracts once sat in
`test_evm.py` looking like ordinary test data. `WATCH` is derived from the registered
sources rather than typed out, so a watch keyword can never name something unimplemented.

## Architecture

**One tick = probe → fetch → diff → events → notify** (`pipeline.py`). The split is the
core cost decision: `probe()` is one cheap request answering "did anything change?", and
only a yes buys the expensive `fetch()`. Anything that makes the probe marker change every
tick silently turns a cheap watcher into an expensive one — see the rate-rounding note below.

**Two extension interfaces, and that is all:**

- `chains/base.py::ChainAdapter` — `scopes()`, `probe()`, `fetch()`. Returns balances and
  transfers. A "scope" is the second axis: one Bitcoin address is one place, one EVM
  address is Ethereum + Arbitrum + Base + BSC at once, each with its own cursor.
  Bitcoin uses the empty scope; EVM uses the network name.
- `protocols/base.py::PositionSource` — `fetch()` returning positions plus an opaque
  marker. Selected by the address's `watch` list; `name` must match the watch keyword.
  One optional method: `trades()`, for a venue that can say what a position *made*. It is
  asked only on a tick where a position shrank, vanished or flipped, never on every tick,
  and a source without it loses nothing.

Registration is one line in `chains/__init__.py::build_adapters` or
`protocols/__init__.py::build_sources`.

**`catalog/` is data, and no tick reads it.** It names one contract per (protocol, network)
that can list that protocol's receipt tokens — an Aave `PoolAddressesProvider`, a Compound
`Comptroller` — so `discover-protocols` can sweep them and propose `tokens:` entries for
what an address actually holds. Storing the market rather than its tokens is the whole
trick: one row stays right through every listing a market adds later. Two rules: **every
address is verified on chain before it is added** (a wrong one reports zero, which reads
exactly like holding nothing — `catalog-check` re-runs the verification), and the file
states which protocols a reader can *ask*, never which are held. A sweep uses
`alchemy_getTokenBalances` with an explicit contract list, not one `eth_call` per token:
at 26 compute units each, a single Aave market's 67 tokens exceed the free tier's 330 CU
per second and the run dies on 429.

**A concentrated-liquidity position is an ERC-721, and holds no amount**
(`protocols/univ3.py`). Because each provider picks a price range, two stakes in one pool
are not interchangeable, so there is no fungible LP token to whitelist — the NFT stores a
liquidity coefficient and two tick bounds, and what it *holds* is computed from the pool's
current price (`protocols/ticks.py`, integer-exact, ported from TickMath). Three consequences
worth keeping: the composition drifts with every trade, so both legs are `accrues` and the
probe marker carries the tick **bucketed to 60** rather than the amounts; uncollected fees
are not in the NFT at all (`tokensOwed` only moves when the position is poked, so it reads
zero on an untouched position) and are read by simulating `collect()` — which only answers
the owner, hence `eth_call_many(sender=…)`; and the one alert worth having is **leaving the
range**, a discrete event meaning the liquidity stopped earning. Forks share the interface
except in one place: Aerodrome Slipstream puts a tick spacing where Uniswap puts a fee tier
and its factory takes `int24`, so it is a separate catalog kind rather than a guess.

**Uniswap v4 keeps that arithmetic and replaces everything around it**
(`protocols/univ4.py`, watch keyword `univ4`). Every pool lives inside one `PoolManager`, so
there is no pool contract and no `slot0()` — the price is read with `extsload` at a slot
computed from the pool id, which is itself `keccak(abi.encode(PoolKey))` and needs no lookup
call. A pool is five fields (currency0, currency1, fee, tickSpacing, hooks): fee and spacing
are independent where v3 tied them, and the hook contract is part of the pool's identity.
`currency0` may be the zero address and mean real ETH. The position NFT is **not**
ERC721Enumerable, so a wallet's positions cannot be listed by asking the contract, and
`eth_getLogs` is not the answer either — the free tier serves it in **ten-block windows**;
`EvmAdapter.owned_nfts` uses Alchemy's NFT index instead. Uncollected fees are not reported
for v4 at all: v3's `collect()` can be simulated, v4's cannot, and computing fee growth from
storage is arithmetic with nothing available to check it against.

**Commands are the same code, read-only** (`bot.py`). `watch` runs the tick loop and a
Telegram long-poll loop as two asyncio tasks in one process; `/portfolio` and `/digest`
render exactly what the CLI renders, `/scan` calls the loop's own tick under a shared lock,
and `/digest` deliberately does not record the total the daily baseline is measured from.
The getUpdates offset lives in `meta`; a cold start drops the backlog rather than replaying
a day of commands. The menu is `setMyCommands` plus inline buttons under each
answer, where Refresh edits that message in place; a reply keyboard was tried and removed,
because a permanent strip above the phone keyboard is the wrong price for six commands.
An edit Telegram refuses ("message is not modified" is a 400, and a 400 here means
`Permanent`) falls back to sending — except that one refusal, which means nothing moved
since the last tap: it answers on the button and leaves the chat alone, because a second
copy of the same numbers is the worst possible reply to "anything new?". Every tap is
acknowledged even when the command failed. Prices for a command are refreshed at
`prices.command_ttl_minutes` rather than the loop's `ttl_minutes`: the background number
is a quota decision (four CoinGecko calls a refresh), while a tap is bounded by a thumb. Bot-facing text is English. PLAN §9 has the rest.

**A message is blocks, not a string.** `Message.body` is the whole text and stays
authoritative, but `Message.blocks` (`models.Block`) carries the same content as sections a
channel can lay out: `mono` asks for a monospace table, `collapsed` for one hidden behind a
tap. Telegram renders those as `<pre>` inside `<blockquote expandable>` and splits at block
boundaries past 4096 characters. Padded columns sent as plain text are a wall — that is what
the digest looked like before, and PLAN §9 records the fix.

**Hyperliquid is one balance, not two.** Perp margin is reserved inside the spot USDC
balance (`hold` == `marginUsed`), so `accountValue` is margin already counted plus PnL:
the total takes spot balances and each perp's unrealised PnL, never `accountValue` and
never a notional. PLAN §6 has the measurements.

**A closed position reports the exchange's realised figure, or no figure at all.**
`events.pnl_usd` is filled from the venue's own fills (`trades()`), summed across the
pieces one order is filled in, and it is what the digest totals for the day. The stored
snapshot's *unrealised* PnL is never used for this: it is up to one interval old and
stalest exactly when a position closes, because the price moving is usually why it closed.
So an unanswerable close says what closed and nothing about money, and `pnl_usd` stays
NULL — which is also why the digest splits on `IS NOT NULL` rather than on a zero. The
figure covers only trades whose position was in the database: one opened and closed inside
a single interval is invisible, and no sum taken from the database can include it.

**Money is always an integer** in base units (`amount_raw` + `decimals`). Floats appear
only in USD figures used for display and thresholds. A float creeping into an amount is a
bug even when the test passes.

**Cursors** live per `(chain, address_id, scope)`. `Cursor.is_fresh` means the address has
never been seen: record the balance as a baseline and alert nothing. Breaking this replays
years of history as notifications. A cursor never parks on an **unconfirmed** item: the
walk stops at `last_item`, so a pending transaction used as the marker becomes the wall
the moment it confirms, and the pending→confirmed requeue dies. Re-reading a few
transactions costs nothing — events deduplicate by uid.

**Events vs notifications are separate tables** so a crash between recording and sending
loses nothing; the next tick delivers what is still pending. Delivery happens once, at the
end of the tick, which is why a failure inside one scope must never escape it: an
exception out of `_scan_scope` or `_scan_positions` skips every remaining address *and*
`_deliver()`, so one provider answering 403 holds back alerts for addresses it has nothing
to do with. Both catch broadly and record the scope as failed. The daily digest is the
same idea in reverse: its baseline is written only when a channel actually accepted it, so
an outage at 9am retries instead of silently losing the day.

## The rules that make it quiet

Most of the work in this project is not fetching data — it is deciding what deserves a
notification. Getting these wrong produces alerts every few hours, forever.

Detail in either view is split by venue first: a Hyperliquid balance is a claim on an
exchange, an address balance is a coin, and one table holding both invites reading the
promise as money. Beacon validators are on-chain — the ETH is staked, not deposited.

| Situation | Mechanism | Result |
|---|---|---|
| gas burns native ETH, no transfer | `BalanceSnapshot.fee_bearing` | negative residual → `accrual`, silent |
| balance grows on its own (Aave rebase, vault share) | `BalanceSnapshot.yield_bearing` | residual → `accrual`, silent |
| a debt grows on its own (borrow interest) | `BalanceSnapshot.debt`, residual under the threshold | `accrual`, silent |
| a debt jumps (borrow or repayment) | `BalanceSnapshot.debt`, residual over the threshold | `position_change` → alerts |
| position grows on its own (validator, HL account) | `Position.accrues` | change → `accrual`, silent |
| money appears with nothing to explain it | neither flag | `anomaly` → alerts |

The USD threshold (`thresholds.notify_usd`) applies to **transfers and position changes
alike**, and measures the *change*, not the holding: a Hyperliquid spot balance drifts by
a fraction of a cent every time funding settles, and for a long time only transfers were
filtered, so an open perp announced itself every tick. Sub-threshold events are still
recorded — they just do not ring. An unpriced change always speaks: silence must never be
the consequence of not knowing what something is worth.

`events.kind` decides the channel: `transfer` and `position_change` alert, `accrual` only
reaches the daily digest, aggregated per asset — the vaults accrue every tick, and a day of
raw accrual rows is thirty lines saying nothing. When a real holding starts alerting every few hours, the answer
is almost always a missing flag rather than a threshold.

## Whitelist mechanics (`TokenCfg`)

Tokens are tracked only if listed in `config/portfolio.yaml`. Removing one from the YAML
and running `init-db` clears its `whitelisted` flag, and the digest counts whitelisted
assets only — otherwise the last balance stays frozen in the total forever, priced at
today's rate. `discover-tokens` *proposes*
candidates and prints YAML; it never writes config, and it never asks DexScreener, because
a scam token's price comes from its own faked pool. Discovery is blind to two things by
construction: balances held inside another contract, and tokens never transferred to you
with a standard `Transfer` event (Alchemy's index is built from those events).

Beyond `chain`/`contract`/`symbol`/`decimals`:

- `coingecko_id` — price as *this* asset instead of by contract address. Needed when the
  contract is listed nowhere (vault shares).
- `rate_call` — a view method giving what one whole share redeems for. Accepts
  `name(uint256)` (fed `1e{share_decimals}`) or `name()`. Implies `yield_bearing`.
- `rate_from` — the contract answering `rate_call`, when it is not the token itself.
- `share_decimals` — when the share's scale differs from the asset's (18-decimal shares of
  6-decimal USDC). Defaults to `decimals`.
- `debt` — the balance is money *owed*, so it is recorded negative and the totals subtract
  it. A variable debt token is an ordinary ERC-20, so this costs no extra request: it rides
  the same probe. Interest and borrowing move the same number, so no flag can separate
  them — the pipeline sizes the change instead. Under `thresholds.notify_usd` it is interest
  and goes to the digest; over it, somebody borrowed or repaid and it alerts. That is why
  `coingecko_id` is required here, and why `debt` and `yield_bearing` together are refused:
  `yield_bearing` is checked first and would silence a six-figure borrow.
- `group` — which holding this counts as in the digest: aBasWETH and WSTETH are both `ETH`.
  Display only, and never guessed from the symbol — ETHFI is not ETH. Empty means the token
  is its own group.

The rate rides in the same JSON-RPC batch as the balances, so it costs no extra round trip.
It is carried into the probe marker **truncated to four decimals**: at full precision it
drifts every block and every tick would trigger the expensive path.

## Testing

`pytest.ini` sets `asyncio_mode = auto`, so async tests need no decorator. **No test
touches the network** — providers are faked with `httpx.MockTransport` and saved response
shapes. Keep it that way; it is what makes the diff logic testable at all.

Test names are sentences describing the behaviour, and docstrings say what breaks in the
real world if the assertion fails. Match that: a test that only restates the code earns
nothing. When a test's premise stops being true (a design change, not a bug), replace it
rather than loosening the assertion.

## Provider traps

`docs/PLAN.md` §5–§6 records the ones already paid for: Hyperliquid spot contexts not
being parallel to the pair list (match on `coin`, and price by token index — the name is
whatever the deployer typed), DexScreener returning pools from
the wrong chain, CoinGecko refusing more than one contract per request without a key,
Alchemy answering 403 for a network merely disabled in the dashboard, beacon withdrawals
being invisible to every transfer API, beaconcha.in's "free" tier being a 1000-request
trial. Check there before concluding a provider cannot do something — and add to it when
you find the next one.
