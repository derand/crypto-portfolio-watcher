# Portfolio Watcher

Watches crypto address balances and notifies when something changes.

**Status: working.** Bitcoin, EVM (Ethereum/Arbitrum/Base/BSC), Hyperliquid,
beacon staking, yield-bearing positions (rebasing receipts and vault shares,
declared in the token whitelist), concentrated-liquidity positions (Uniswap v3/v4
and forks), USD prices and a daily digest.
Next: ntfy/Discord and reward claims.

Read-only by design: no private keys, no signing, ever. The watcher only ever
reads public data, so the worst an attacker gets from this machine is a list of
addresses - which is why the configuration stays out of the repository.

## Requirements

Python 3.12+ and the packages in `requirements.txt` (`httpx`, `pydantic`,
`PyYAML`, `eth-utils`), or Docker. No database server: state is one SQLite file.

Free-tier API keys are enough for a handful of addresses: Alchemy for EVM,
Etherscan for beacon withdrawals, CoinGecko for prices, a Telegram bot token to
be notified. `.env.example` lists them and says which phase needs which.

## Running

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env                                     # TELEGRAM_*, ALCHEMY_API_KEY
cp config/portfolio.example.yaml config/portfolio.yaml   # fill in your own addresses

./pw config-check     # validate the config and see exactly what would be polled
./pw init-db          # create the schema and sync addresses/tokens from the YAML
./pw test-notify      # send a test message through every enabled channel
./pw scan             # one tick: poll addresses, record changes, notify
./pw watch            # the same on a loop, at the configured interval, plus the bot
./pw bot              # the bot only, without the scan loop (for debugging)
./pw portfolio        # what is held now, at today's prices (--send to deliver it)
./pw digest           # the daily summary (--dry-run to only look at it)
./pw status           # what the database currently holds
./pw discover-tokens  # propose tokens for the whitelist (never writes the config)
                      # --show-unpriced: also what there is no price for
./pw discover-protocols # propose positions the token index cannot see
./pw catalog-check    # ask every catalog entry to enumerate itself on chain
./pw telegram-chat-id # find your TELEGRAM_CHAT_ID (needs the token only)
```

`./pw` installs nothing. It runs the CLI with the first interpreter it finds:
`$PW_PYTHON`, then `./.venv`, then `../venv` (a virtualenv shared with sibling
projects), then `python3` from `PATH`.

`-v` turns on the verbose log, `-c` / `-e` point at other config and `.env` paths.
The verbose flag goes *before* the subcommand: `./pw -v scan`.

## Configuration

The example addresses in `config/portfolio.example.yaml` are `enabled: false`.
Put in your own address and set `enabled: true` - until then `scan` polls nothing.

Tokens are tracked only if they appear in the whitelist, so a new one needs
`init-db` (to sync the config into the database) *and* one `scan` (to fetch it);
without the scan the digest keeps showing the previous picture.

## Docker

```bash
cp .env.example .env                                     # keys, plus TZ/PUID if needed
cp config/portfolio.example.yaml config/portfolio.yaml   # addresses

docker compose up -d --build     # watch: the tick loop plus the command bot
docker compose logs -f
docker compose down

docker compose --profile cli run --rm pw config-check    # one-off commands
docker compose --profile cli run --rm pw -v scan
docker compose --profile dev run --rm tests              # the test suite in the container
```

`data/` and `config/` are bind-mounted from the host, so the database survives
`down`/`up` and a rebuild; `.env` and `config/portfolio.yaml` never enter the image.
The image runs as uid/gid 1000; if `id -u` on the host says otherwise, set
`PUID`/`PGID` in `.env` so `data/` stays writable.
The container's timezone is `UTC`, so `digest.hour: 9` means 9:00 UTC; for local
time set `TZ` in `.env` to your own zone (any tzdata name, e.g. `Europe/Berlin`).
`init-db` runs automatically before `watch`/`scan`/`bot` (it only syncs the config
into the database and fetches nothing over the network) - after editing
`portfolio.yaml` a `docker compose restart` is enough, followed by one `scan`.
Disable it with `INIT_DB_ON_START=false`.

## Telegram commands

While `./pw watch` is running, the bot answers `/portfolio`, `/digest`, `/status`,
`/health`, `/scan` and `/help` in the `TELEGRAM_CHAT_ID` chat. Every answer carries
inline buttons with the next steps; "🔄 Refresh" rewrites that same message. Messages
from other chats are ignored. Turn it off with `notify.telegram.commands: false` -
keep it enabled in exactly one place: two processes sharing one token split the
updates between them.

## Tests

```bash
.venv/bin/python -m pytest -q
```

No test touches the network: every provider is faked with saved response shapes.

## Where things live

| Path                      | What                                            |
|---------------------------|-------------------------------------------------|
| `config/portfolio.yaml`   | addresses, `watch` profiles, the token whitelist |
| `.env`                    | API keys and bot tokens                          |
| `data/portfolio.db`       | SQLite: balances, events, history                |
| `src/portfolio/chains/`   | chain adapters (Bitcoin, EVM)                    |
| `src/portfolio/protocols/`| Hyperliquid, beacon, concentrated liquidity      |
| `src/portfolio/notify/`   | Telegram, later ntfy and Discord                 |
| `src/portfolio/discover.py`| whitelist candidates, run by hand               |
| `src/portfolio/catalog/`  | protocol entry points; data, polled by no tick   |
| `Dockerfile`, `docker-compose.yml` | running in a container                  |
| `requirements.txt`        | runtime dependencies, pinned                     |

`config/portfolio.yaml`, `.env` and `data/` stay out of git (see `.gitignore`),
because they hold addresses and keys. So does the design record in `docs/`, for
the same reason: it discusses the configuration it explains.
