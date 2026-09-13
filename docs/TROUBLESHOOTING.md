# Troubleshooting

Work top to bottom. After each change, **fully quit** the wallet, start it
once, then refresh [http://127.0.0.1:8080](http://127.0.0.1:8080).

---

## Yellow banner / “node offline”

The explorer cannot log in to the wallet.

### 1. Is the wallet actually running?

Task Manager (Ctrl+Shift+Esc) should show **xcoin-qt** (GUI) **or**
**xcoind** (no window) — not both.

### 2. Did you set `server=1` in the file 1.0.13 reads?

X Coin **1.0.13** double-click reads `xcoin.conf` **next to**
`X Coin Wallet.exe`, not only `%APPDATA%\XCoin\xcoin.conf`.

Open both (if they exist) and add a line that is exactly:

```
server=1
```

No `#` in front of it. Save. Fully quit the wallet, start it once.
In PowerShell, `netstat -ano | findstr 38442` should show LISTENING.

### 3. Cookie missing

After a good start, Windows should have:

```
%APPDATA%\XCoin\.cookie
```

Show hidden files: File Explorer → View → Show → Hidden items.

If the file is missing:

- You opened a **second** wallet while the first was still running. The
  second one fails to bind port 38442 and can delete the cookie. Quit
  **all** X Coin processes, wait 5 seconds, start **one** wallet.
- You started Practice Wallet (regtest). Cookie is then under
  `%APPDATA%\XCoin\regtest\.cookie`.

### 4. Port 38442 is in use by a dead start

In PowerShell:

```powershell
netstat -ano | findstr 38442
```

If you see `LISTENING` but `.cookie` is gone, the process holding the
port is stale. End `xcoin-qt` and `xcoind` in Task Manager, wait, start
one wallet.

### 5. Cookie still flaky — use a password

Follow **Optional: password instead of cookie** in [SETUP.md](SETUP.md).
Username and password must match in the `xcoin.conf` next to
**X Coin Wallet.exe** (1.0.13) **and** `explorer.toml`.

---

## `start.bat` closes immediately

1. Open the explorer folder in File Explorer.
2. Click the address bar, type `cmd`, Enter.
3. Run:

   ```bat
   start.bat
   ```

4. Read the message.

Common causes:

| Message | Fix |
| --- | --- |
| `python` is not recognized | Reinstall Python and tick **Add to PATH** |
| `No module named explorer` | Run `start.bat` from the explorer folder, not from `explorer\` inside it |
| Permission / pip errors | Run the same `cmd` **not** as Administrator first; if it still fails, try “Run as administrator” once |

---

## Browser shows “Unable to connect” to port 8080

The explorer process is not running. Double-click `start.bat` again and
leave that window open. Default address is
[http://127.0.0.1:8080](http://127.0.0.1:8080) — not `https`.

---

## Height stays 0

That can be correct. Height 0 is genesis (12 September 2026, 3:16 AM ET).
It is **not** a lottery payday. Height 1 is the first lottery block, once
an X Verified node produces it.

If peers are 0, the wallet is not connected to the mesh. The official
wallet packages ship `addnode` / `seednode`. Do not invent a host.

---

## Lottery “0 active”

Live “active now” is `getlotteryinfo.stamped_handles` when the connected
node has it (the baked seed prints every main block). Otherwise the
explorer shows the last indexed block’s coinbase `XVA1` handles. It does
**not** treat `getactivenodes` as the hat — a player wallet often omits
itself.

Historical winners come from each block’s coinbase `XVA1` (handle + id +
stamp). The winner is who got paid. Payout addresses change; `@handle`
does not. `XID1` is the asset-root claim, not lottery identity.

---

## “Wallet already running” / cannot start GUI

`xcoind` is still running. End it in Task Manager, then open the GUI.
One data folder, one program.

---

## I changed `txindex=1` and the wallet refuses to start

Indexes are set on first start. Changing them later can require
`-reindex` (slow, advanced). Remove the new index lines from `xcoin.conf`
and start normally. This explorer can index confirmed blocks without
`txindex`.
