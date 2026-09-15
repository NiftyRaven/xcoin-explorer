# Setup guide (start here)

This explorer is a **website on your computer**. It does not hold coins. It
reads a running **X Coin wallet / node** and shows blocks, assets, lottery
winners, and eligible `@handles` in a browser.

You need **X Coin 1.0.14 Light or 1.0.15 Heavy**. Same chain. Do not stay
on 1.0.12 if you need Claim My Asset to confirm. 1.0.13 peers still
follow this chain. **Share lottery wins** (host/guest split of a mature
payout) is a wallet send — guests never enter the hat. Market listings
in Heavy are wallet gossip, not explorer consensus.

---

## One click (recommended)

### Windows

1. Unzip this explorer (or clone the repo).
2. Double-click **`SETUP.bat`**.
3. If Windows SmartScreen warns, choose **More info → Run anyway**.
4. Finish any first-run **12-word** screen in **X Coin Wallet**.
5. Browse [http://127.0.0.1:8080](http://127.0.0.1:8080).

`SETUP.bat` will try to:

- Install **Python 3.11+** with `winget` if Python is missing (or open
  [python.org](https://www.python.org/downloads/windows/) if `winget` is not there)
- Find **X Coin Wallet.exe** if it is already installed, or download the
  **latest official** Windows zip from
  [NiftyRaven/x-coin Releases](https://github.com/NiftyRaven/x-coin/releases)
  into `%LOCALAPPDATA%\XCoin-Wallet\<version>` (1.0.15 Heavy today;
  Light 1.0.14 also works)
- Add only these **template** lines to the wallet config (no passwords):

  ```
  server=1
  rpcbind=127.0.0.1
  rpcallowip=127.0.0.1
  ```

- Start the wallet if it is not running, then start this explorer

Next time you only need **`start.bat`** (wallet already open).

### Linux (x86_64)

```bash
chmod +x setup.sh start.sh
./setup.sh
```

Same idea: install Python if needed (`apt` / `dnf`), use the wallet if
it is already installed, otherwise download the latest official Linux
tarball into `~/.local/share/XCoin-Wallet/<version>`, add `server=1`
(no passwords), start the wallet, then start the explorer.

Next time: `./start.sh`.

### What one-click will never do

- It will **not** write `rpcuser` / `rpcpassword`
- It will **not** copy `.cookie`, `.pem`, or your 12-word seed
- It will **not** invent a peer host (the official wallet zip already
  has `addnode` / `seednode` for the baked seed)
- It will **not** spend coins

If the wallet is already installed somewhere else, set
`XCOIN_WALLET` to the full path of `X Coin Wallet.exe` /
`X Coin Wallet` and run setup again.

---

## What you need (manual path)

| Item | Why |
| --- | --- |
| **Windows** or **Linux x86_64** | What the wallet supports |
| **X Coin wallet 1.0.14 Light / 1.0.15 Heavy** | [github.com/NiftyRaven/x-coin/releases](https://github.com/NiftyRaven/x-coin/releases) |
| **Python 3.11 or newer** | Runs the explorer |
| A web browser | Chrome, Firefox, Edge, … |

You do **not** need to compile anything. You do **not** paste keys or
seeds into the explorer.

---

## Manual Step 1 — Install Python

### Windows

1. Open [https://www.python.org/downloads/](https://www.python.org/downloads/).
2. Download the latest **Windows installer (64-bit)**.
3. Run it.
4. **Check the box** “Add python.exe to PATH” at the bottom of the first screen.
5. Click **Install Now**.
6. Close and reopen any Command Prompt / PowerShell windows.

Check it worked: press `Win+R`, type `cmd`, Enter, then:

```bat
python --version
```

You should see `Python 3.11` or higher. If Windows opens the Microsoft Store
instead, install Python from python.org and tick **Add to PATH**.

### Linux

Debian / Ubuntu:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
python3 --version
```

---

## Manual Step 2 — Get the explorer files

### Option A — Git

```bat
git clone https://github.com/NiftyRaven/xcoin-explorer.git
cd xcoin-explorer
```

### Option B — Release zip (no Git)

1. Open [xcoin-explorer Releases](https://github.com/NiftyRaven/xcoin-explorer/releases).
2. Download **XFER-Explorer-1.3.1.zip**.
3. Right-click → **Extract All**.
4. Open the folder and double-click **SETUP.bat** (Windows) or run
   `./setup.sh` (Linux).

---

## Manual Step 3 — Let the wallet talk to the explorer (RPC)

The wallet is a closed box until you turn on **RPC** (a local-only API).
The explorer uses that. Nothing is opened to the internet.

### 3a. Fully quit the wallet

Close **X Coin Wallet**. If an icon remains in the system tray (near the
clock), right-click it → Exit. Wait two seconds. Task Manager should show
no `xcoin-qt` or `xcoind`.

You cannot have the GUI wallet **and** `xcoind` running at the same time.
They share one data folder.

### 3b. Open the wallet’s data folder

**Windows**

1. Press `Win + R`.
2. Type:

   ```
   %APPDATA%\XCoin
   ```

3. Press Enter. File Explorer opens `C:\Users\<you>\AppData\Roaming\XCoin`.

**Linux**

```bash
mkdir -p ~/.xcoin
xdg-open ~/.xcoin
```

Practice / “Practice Wallet” uses a subfolder named `regtest`. Main XFER
uses the folder above, not `regtest`.

### 3c. Put `server=1` where the wallet actually reads it

**X Coin 1.0.13+** (Light 1.0.14 and Heavy 1.0.15) double-click reads
the `xcoin.conf` **next to** `X Coin Wallet.exe` (Windows) or **X Coin
Wallet** (Linux). If that file has no `server=1`, port 38442 never opens
and the explorer shows “node offline”.

Do this in **every** `xcoin.conf` you have:

1. The file in the extracted 1.0.14 wallet folder (same folder as the
   start).
2. The data-folder file from 3b (`%APPDATA%\XCoin\xcoin.conf` or
   `~/.xcoin/xcoin.conf`). Create it if it is missing.

Add this line (or copy from `config/xcoin.conf.example`):

```
server=1
```

No `#` in front. Save.

That is enough for most people. The wallet writes a secret **cookie**
next start (`%APPDATA%\XCoin\.cookie`). The explorer finds it. You never
type it.

If you later set `rpcuser` / `rpcpassword`, put those lines in the
**same** wallet-folder `xcoin.conf` the start reads, and copy only
those two values into a local `explorer.toml`. Never commit that file.

### 3d. Open the wallet **once**

Start **X Coin Wallet.exe** (or **X Coin Wallet** on Linux) as you always
do. Wait until it finishes loading.

Check that a cookie appeared:

- Windows: `%APPDATA%\XCoin\.cookie`  (it is a hidden file)
- Linux: `~/.xcoin/.cookie`

In File Explorer: View → Show → Hidden items.

If `.cookie` is missing, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Manual Step 4 — Start the explorer only

### Windows

1. Open the explorer folder you cloned or unzipped.
2. Double-click **`start.bat`**.
3. The first run may take a minute (`Creating virtualenv…` then pip install).
4. A browser tab should open at [http://127.0.0.1:8080](http://127.0.0.1:8080).

If a window flashes and closes: open `cmd` in that folder and run
`start.bat` so you can read the error.

### Linux

```bash
cd xcoin-explorer
chmod +x start.sh
./start.sh
```

Then open [http://127.0.0.1:8080](http://127.0.0.1:8080).

Leave **both** the wallet and the `start.bat` / `start.sh` window running.
The explorer only works while the node is up.

---

## Did it work?

On the Home page you should see:

- A green pill: **node · mainnet** (or practice/regtest)
- A height number (0 is normal only at genesis; after launch it should climb)

Then try **Members** to browse eligible `@handles`, **Assets** for IPFS,
and **Lottery** for the minute draw.

If you see a yellow banner about RPC, read
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Optional: password instead of cookie

Use this only if cookie login keeps failing. Pick your own values.
Never commit them.

1. Pick a username and a **long random password**. Do not reuse a website password.
2. In the wallet `xcoin.conf` the start actually reads (next to
   **X Coin Wallet.exe**, and the data-folder file if you use both) add:

   ```
   server=1
   rpcuser=YOUR_RPC_USERNAME
   rpcpassword=YOUR_LONG_RANDOM_PASSWORD
   rpcallowip=127.0.0.1
   ```

3. In the explorer folder copy `explorer.toml.example` to **`explorer.toml`**.
4. Set the **same** username and password:

   ```toml
   [rpc]
   host = "127.0.0.1"
   port = 38442
   user = "YOUR_RPC_USERNAME"
   password = "YOUR_LONG_RANDOM_PASSWORD"
   ```

5. Fully quit the wallet, start it once, then start the explorer.

Never commit `explorer.toml` or paste those values in a public issue.

---

## Optional: practice chain

Practice Wallet uses **regtest** (`-regtest`). Data is
`%APPDATA%\XCoin\regtest` (Windows) or `~/.xcoin/regtest` (Linux).
RPC port is **28442**. Put `server=1` in **that** `xcoin.conf`.
The explorer tries 28442 automatically if 38442 is not up.

---

## What you should not do

- Do not run `xcoind` and the GUI wallet at the same time.
- Do not share `.cookie`, `explorer.toml`, or `rpcpassword`.
- Do not put your 12-word seed in any explorer file.
- Do not change the X Coin **chain** source to use this tool. The explorer
  only **reads** the node.

---

## Ports (for the curious)

| What | Port |
| --- | --- |
| Wallet RPC (main) | 38442 |
| Wallet P2P (main) | 38443 |
| Practice RPC | 28442 |
| This explorer website | 8080 |

RPC is bound to localhost. Other computers on your network cannot use it
unless you change that (do not).
