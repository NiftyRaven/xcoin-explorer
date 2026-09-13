# XFER Explorer

A **local** block explorer, asset explorer, and lottery browser for
[X Coin (XFER) 1.0.13+](https://github.com/NiftyRaven/x-coin).

One website on your machine. Search a height, transaction, `X…` address,
asset name, or `@handle`. Click an asset to see holders, activity, and
IPFS images or video. See who won each minute’s lottery.

This is **not** a wallet. It cannot spend coins. It only reads a node you
already run.

**New here?** Double-click **[SETUP.bat](SETUP.bat)** (Windows) or run
**[setup.sh](setup.sh)** (Linux). Details:
**[START HERE.txt](START HERE.txt)** · **[docs/SETUP.md](docs/SETUP.md)**.
Stuck? **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**.

Use wallet **1.0.13 or newer**. Do not stay on 1.0.12 if you need Claim
My Asset to confirm.

---

## One-click start

| | |
| --- | --- |
| Windows | Double-click `SETUP.bat` (first time) then `start.bat` |
| Linux x86_64 | `chmod +x setup.sh start.sh && ./setup.sh` |

The setup script installs Python if needed, finds or downloads official
X Coin **1.0.13**, adds `server=1` (no passwords), starts the wallet, and
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
| Assets | Identity roots, `NAME/CHILD` subs, `NAME#tag` uniques; click through for holders, activity, and IPFS image/video |
| Lottery | Live draw, active `@handles`, history, leaderboard |
| Members | Browse every eligible `@handle`, or search any handle |
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
the 1.0.13 start actually reads **and** in a local `explorer.toml`
(see SETUP). Never put those values in git.

---

## Requirements

- Python 3.11+
- [X Coin wallet 1.0.13+](https://github.com/NiftyRaven/x-coin/releases) with `server=1`
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
