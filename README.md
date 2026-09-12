# XFER Explorer

A **local** block explorer, asset explorer, and lottery browser for
[X Coin (XFER)](https://github.com/NiftyRaven/x-coin).

One website on your machine. Search a height, transaction, `X…` address,
asset name, or `@handle`. See who won each minute’s lottery.

This is **not** a wallet. It cannot spend coins. It only reads a node you
already run.

**New here?** Follow **[docs/SETUP.md](docs/SETUP.md)** from the top.
Stuck? **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**.

---

## Quick start (Windows)

1. Install [Python 3.11+](https://www.python.org/downloads/) (tick **Add python.exe to PATH**).
2. Install the [X Coin wallet](https://github.com/NiftyRaven/x-coin/releases).
3. Fully quit the wallet. Win+R → `%APPDATA%\XCoin` → open or create `xcoin.conf` → add:

   ```
   server=1
   ```

4. Start the wallet **once**. Wait until it loads.
5. In this folder, double-click `start.bat`.
6. Browse [http://127.0.0.1:8080](http://127.0.0.1:8080).

Linux: `./start.sh` after the same `server=1` line in `~/.xcoin/xcoin.conf`.

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
| Transaction | Inputs, outputs, assets, identity claims (`XID1`) |
| Address | XFER balance, assets, lottery wins |
| Assets | Identity roots, `NAME/CHILD` subs, `NAME#tag` uniques |
| Lottery | Live draw, active `@handles`, history, leaderboard |
| Holders | Richest XFER addresses |
| Mempool / Network | Unconfirmed txs and peers |

Height **0** is genesis (12 Sep 2026, 3:16 AM America/New_York). It is not
a payday. Lottery winners start at height **1**.

---

## How it talks to the chain

```
[ X Coin Wallet.exe  or  xcoind ]  --RPC 127.0.0.1:38442-->  [ this explorer ]  -->  browser :8080
```

Use **either** the GUI wallet **or** `xcoind`, not both. They share one
data directory.

Default login is the wallet **cookie** (`%APPDATA%\XCoin\.cookie`). If
that fails, set matching `rpcuser` / `rpcpassword` in the wallet config
and in a local `explorer.toml` (see SETUP).

---

## Requirements

- Python 3.11+
- A synced (or syncing) X Coin node with `server=1`
- Ports: node RPC **38442**, explorer **8080** (localhost)

```bat
python -m pytest
```

---

## Security

- RPC stays on `127.0.0.1`. Do not expose 38442 to the internet.
- Do not put 12-word seeds, OAuth tokens, or RPC passwords in git.
- `explorer.toml` and `data/` are gitignored on purpose.

---

## License

MIT. X Coin itself is a separate project:
[NiftyRaven/x-coin](https://github.com/NiftyRaven/x-coin).
