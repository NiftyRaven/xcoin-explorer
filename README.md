# XFER Explorer

A block explorer for [X Coin (XFER)](https://github.com/NiftyRaven/x-coin)
(Light or Heavy). Use the [latest release](https://github.com/NiftyRaven/x-coin/releases).

It runs on your machine. Search a height, transaction, `X…` address,
asset name, or `@handle`. Open an asset for holders and its file.
See who won each minute’s lottery.

This is not a wallet. It cannot spend coins. It only reads a node you
already run.

**New here?** Double-click **[SETUP.bat](SETUP.bat)** (Windows) or run
**[setup.sh](setup.sh)** (Linux). Details:
**[START HERE.txt](START HERE.txt)** · **[docs/SETUP.md](docs/SETUP.md)**.
Stuck? **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**.

Light and Heavy are the same chain. Setup downloads the newest official
zip from [Releases](https://github.com/NiftyRaven/x-coin/releases).

---

## One-click start

| | |
| --- | --- |
| Windows | Double-click `SETUP.bat` (uses an installed wallet, or downloads the latest) |
| Linux x86_64 | `chmod +x setup.sh start.sh && ./setup.sh` |

The setup script installs Python if needed, uses **X Coin Wallet if it
is already installed**, otherwise downloads the **newest official** zip
by version tag from [NiftyRaven/x-coin Releases](https://github.com/NiftyRaven/x-coin/releases)
(not GitHub’s “latest” flag — that can stay on an older Light zip),
adds `server=1` (no passwords), starts the wallet, and
opens [http://127.0.0.1:8080](http://127.0.0.1:8080).

Templates (no secrets in this repo):

- Wallet: [`config/xcoin.conf.example`](config/xcoin.conf.example)
- Explorer (optional password login): [`explorer.toml.example`](explorer.toml.example)

Copy examples locally. Never commit `explorer.toml`.

---

## What you can browse

| Page | Contents |
| --- | --- |
| Home | Height, supply, latest blocks, live lottery |
| Blocks | Every height; coinbase lottery payouts from height 1 |
| Transaction | Inputs, outputs, assets; coinbase identity is `XVA1` `@handle` (not `XID1`) |
| Address | XFER balance, assets, lottery wins |
| Assets | Roots, `NAME/CHILD` subs, `NAME#tag` uniques; holders and files |
| Lottery | Live draw, active `@handles`, history, leaderboard, guest shares |
| Members | Browse every eligible `@handle`, or search any handle |
| Stats | Emission, hat size, and win share |
| Holders | Richest XFER addresses |
| Mempool / Network | Unconfirmed txs and peers |

Height **0** is genesis (12 Sep 2026, 3:16 AM America/New_York). It pays
nothing. Lottery winners start at height **1**.

---

## How it talks to the chain

```
[ X Coin Wallet.exe  or  xcoind ]  --RPC 127.0.0.1:38442-->  [ this explorer ]  -->  browser :8080
```

The indexer and live UI poll about **once per block** (~60s,
`BLOCK_TIME_SECONDS` / `poll_seconds` in `explorer.toml`). Catch-up
does not sleep. The browser checks `/api/tip` on that cadence and
re-renders only when the tip hash or height changes. The one-second
lottery countdown is a clock only — it does not refetch.

Use **either** the GUI wallet **or** `xcoind`, not both. They share one
data directory.

Default login is the wallet **cookie** (`%APPDATA%\XCoin\.cookie`). If
that fails, set matching `rpcuser` / `rpcpassword` in the wallet config
the wallet start actually reads **and** in a local `explorer.toml`
(see SETUP). Never put those values in git.

---

## Public API: coin supply

Read-only endpoints for listing sites (CoinGecko, CoinMarketCap, and similar).
The live explorer serves them at `https://explorer.xferchain.net`.

| Endpoint | Returns |
| --- | --- |
| `GET /api/supply/circulating` | Circulating XFER, plain text |
| `GET /api/supply/total` | Total XFER in existence, plain text |
| `GET /api/supply/max` | Maximum XFER that will ever exist, plain text |
| `GET /api/supply` | JSON: all three, the height, atom amounts, and each burn address |

The plain-text endpoints return one decimal number in XFER and nothing else:
no units, no thousands separators, no trailing zeros (for example `177465000`
or `177465000.12345678`). They send `Content-Type: text/plain`,
`Access-Control-Allow-Origin: *`, and `Cache-Control: public, max-age=60`.
The explorer recomputes the numbers at most once a minute. All math is in whole
atoms (1 XFER = 100,000,000 xferons), so there is no float rounding.

Definitions:

- **Issued**: block subsidy paid from height 1 through the indexed tip
  (5,000 XFER per block in the first era; genesis pays nothing). This is
  `supply_atoms` in `/api/status`.
- **Total** = issued minus XFER held at the chain's burn addresses. The
  addresses come from `chainparams.cpp` (asset issue, reissue, sub, unique,
  message channel, qualifier, sub qualifier, restricted, null tag, and global
  burn). Nobody has a key for them, so those coins can never move.
- **Circulating** = total. X Coin has no premine, team, vesting, or locked
  coins, and the explorer treats nothing else as unspendable.
- **Max** = every coin the halving schedule will ever pay
  (`lifetime_atoms` in `/api/stats`, about 21 billion XFER).

XFER sent to an OP_RETURN output is destroyed too, but the indexer stores those
outputs with zero value, so it cannot subtract them. Ordinary OP_RETURN memos
carry no XFER.

---

## Requirements

- Python 3.11+
- [Latest X Coin release](https://github.com/NiftyRaven/x-coin/releases), Light or Heavy, with `server=1`
- Ports: node RPC **38442**, explorer **8080** (localhost)

```bat
python -m pytest
```

---

## Security

- RPC stays on `127.0.0.1`. Do not expose 38442 to the internet.
- Do not put 12-word seeds, OAuth tokens, RPC passwords, or `.pem` keys in git.
- `explorer.toml` and `data/` are gitignored on purpose.

---

## License

MIT. X Coin itself is a separate project:
[NiftyRaven/x-coin](https://github.com/NiftyRaven/x-coin).
