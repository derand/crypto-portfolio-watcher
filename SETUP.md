# Setup: keys and a free server

Everything the watcher talks to has a free tier, and a handful of addresses stays well
inside all of them. The server can be free too. This page is the shortest path to both.

## API keys

All of them go into `.env` (copy `.env.example`). Only Telegram is strictly required;
each of the others unlocks one part of the watcher.

| Variable | Needed for | Free tier |
|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | every notification and the bot | free, no limit that matters here |
| `ALCHEMY_API_KEY` | EVM addresses: balances, tokens, transfers, NFT positions | monthly compute-unit allowance, no card |
| `COINGECKO_DEMO_KEY` | USD prices | Demo plan: 30 calls/min, 10,000 calls/month |
| `ETHERSCAN_API_KEY` | beacon validator withdrawals only | 100,000 calls/day |

No key at all: mempool.space (Bitcoin), Hyperliquid, a public beacon node, DexScreener.

### Telegram

1. Open [@BotFather](https://t.me/BotFather), send `/newbot`, pick a name. The reply holds
   the token → `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message, then run `./pw telegram-chat-id` → `TELEGRAM_CHAT_ID`.

### Alchemy

1. Sign up at [dashboard.alchemy.com](https://dashboard.alchemy.com), create an app.
2. **Enable every network you list under `chains:`** in the app's network settings:
   Ethereum, Arbitrum, Base, BNB. A network left disabled answers `403` to every request,
   which looks exactly like the network not being supported.
3. Copy the API key → `ALCHEMY_API_KEY`.

### CoinGecko

1. Sign up at [coingecko.com/en/api/pricing](https://www.coingecko.com/en/api/pricing),
   choose the free **Demo** plan.
2. Developer Dashboard → create a key → `COINGECKO_DEMO_KEY`.

Without a key CoinGecko accepts one contract per request and prices silently fall back to
DexScreener. Keep `prices.ttl_minutes: 60` in `config/portfolio.yaml`: a refresh costs
about four calls, and every 15 minutes that is ~11,500 a month, over the Demo quota.

### Etherscan

Only if an address watches `beacon`. Sign up at
[etherscan.io/myapikey](https://etherscan.io/myapikey), add a key → `ETHERSCAN_API_KEY`.
One key covers every chain through the V2 API.

### Staying inside the limits

At a 15-minute interval a tick costs a few dozen requests, most of them the cheap probe,
because the expensive fetch runs only when something changed. Check each dashboard's usage
page after the first couple of days; if one is climbing, raise `interval_minutes` before
touching anything else.

## A free server: Oracle Cloud Always Free

The watcher needs one process that runs forever and a disk that keeps `data/`. Serverless
platforms offer neither, so the fit is a small VM. Oracle's Always Free tier gives an ARM
VM with up to 4 OCPU / 24 GB and 200 GB of block storage, with no time limit.

### Account

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com). A card is required; the
   verification is a temporary hold of about $1 that is released.
2. **The home region is permanent**, and Always Free resources exist only there. Popular
   regions often have no ARM capacity; a less popular one is easier.
3. Enrol two sign-in methods (a passkey and a TOTP app, say) and save several bypass codes
   in a password manager. Losing access to this account means losing the VM.

The account starts as a 30-day Free Trial. When the trial ends, anything that is not
**Always Free-eligible** is stopped and deleted, so create only resources carrying that
label, even while the credits last.

### Network

Create it before the instance: the instance wizard's inline network often leaves the
public IP switch disabled.

**Networking → Virtual Cloud Networks → Start VCN Wizard → Create VCN with Internet
Connectivity**, defaults. That gives a public subnet, an internet gateway, and SSH (22)
open. The watcher needs no inbound port besides SSH: Telegram long-polling and every API
are outbound.

### SSH key

On your own machine, so the private key never passes through a browser:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/oracle -C "oracle"
pbcopy < ~/.ssh/oracle.pub        # macOS; elsewhere print it and copy
```

### Instance

**Compute → Instances → Create instance**:

| Field | Value |
|---|---|
| Image | Oracle Linux 9 or Ubuntu 24.04, **aarch64** build |
| Shape | Ampere → **VM.Standard.A1.Flex**, 1 OCPU / 6 GB (plenty for the watcher; the rest of the free allowance stays available) |
| Networking | existing VCN → **public subnet**, assign public IPv4 |
| SSH keys | Paste public key |
| Boot volume | defaults (~47 GB, Balanced), Oracle-managed encryption |

Then `ssh opc@<public-ip>` (Oracle Linux) or `ssh ubuntu@<public-ip>` (Ubuntu).

### "Out of capacity"

The usual answer for A1. In order of what helps:

- **Save as stack** on the create form. Retrying is then Resource Manager → Stacks →
  Apply, without refilling anything. Nights and early mornings succeed more often.
- **Upgrade to Pay As You Go.** A1 capacity comes much more easily, and Always Free usage
  is still $0. Set a budget alert first (Billing → Budgets, $1) so a mistake shows up in
  a day, not on a statement. The upgrade may place a larger temporary hold on the card.
- **VM.Standard.E2.1.Micro** (x86, 1 GB) is Always Free too and usually available. It runs
  the watcher; add a 1–2 GB swap file before `docker compose build`.

**An idle Always Free instance may be reclaimed** (CPU, network and memory all under 20%
over 7 days, which describes a watcher exactly). A Pay As You Go account is exempt.

Once the VM is up, the [Docker section of the README](README.md#docker) is the rest.
