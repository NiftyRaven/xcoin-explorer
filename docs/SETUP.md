# Setup guide (start here)

This explorer is a **website on your computer**. It does not hold coins. It
reads a running **X Coin wallet / node** and shows blocks, assets, and
lottery winners in a browser.

You will do three things:

1. Install Python (once).
2. Tell the X Coin wallet to answer local questions (RPC).
3. Start the explorer.

---

## What you need

| Item | Why |
| --- | --- |
| A computer on **Windows** or **Linux** | This is what the wallet supports |
| The **X Coin wallet** already installed | [github.com/NiftyRaven/x-coin](https://github.com/NiftyRaven/x-coin) |
| **Python 3.11 or newer** | Runs the explorer |
| A web browser | Chrome, Firefox, Edge, … |

You do **not** need to compile anything. You do **not** paste keys or
seeds into the explorer.

---

## Step 1 — Install Python

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

## Step 2 — Get the explorer files

### Option A — Git (if you have Git)

```bat
git clone https://github.com/NiftyRaven/xcoin-explorer.git
cd xcoin-explorer
```

### Option B — Zip (no Git)

1. Open the GitHub page: [github.com/NiftyRaven/xcoin-explorer](https://github.com/NiftyRaven/xcoin-explorer).
2. Click the green **Code** button → **Download ZIP**.
3. Right-click the zip → **Extract All**.
4. Open the folder that appears (`xcoin-explorer-main` or similar).

---

## Step 3 — Let the wallet talk to the explorer (RPC)

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

### 3c. Edit (or create) `xcoin.conf`

In that folder, look for a file named **`xcoin.conf`**.

- If it exists, open it with Notepad (Windows) or any text editor.
- If it does not exist, create a new text file named exactly `xcoin.conf`
  (not `xcoin.conf.txt`). In Notepad: File → Save As → “All files” →
  name `xcoin.conf`.

Add **this one required line** at the bottom (or copy from
`config/xcoin.conf.example` in this repo):

```
server=1
```

Save the file.

That is enough for most people. The wallet will write a secret **cookie**
file next start. The explorer finds it automatically. You never type it.

### 3d. Open the wallet **once**

Start **X Coin Wallet.exe** (or **X Coin Wallet** on Linux) as you always
do. Wait until it finishes loading.

Check that a cookie appeared:

- Windows: `%APPDATA%\XCoin\.cookie`  (it is a hidden file)
- Linux: `~/.xcoin/.cookie`

In File Explorer: View → Show → Hidden items.

If `.cookie` is missing, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Step 4 — Start the explorer

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
- A height number (0 is normal before the first lottery block)

If you see a yellow banner about RPC, read
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Optional: password instead of cookie

Use this only if cookie login keeps failing.

1. Pick a username and a **long random password**. Do not reuse a website password.
2. In the wallet `xcoin.conf` add:

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
