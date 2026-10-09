"""Which X handle an address belongs to, and the proof for it.

Only provable links are written. Every row in ``address_links`` names one proof:

* ``claim``   the address holds the handle's claimed root (the ``identities`` row).
* ``wallet``  the handle's signed-in Launch vault signed a message with this address's key.
              Launch verifies the signature; the explorer reads the list from the Launch relay.
* ``owner``   the address held a claimed owner token (``ROOT!``, ``ROOT/SUB!``) on an unbroken
              path from the claim. The path goes on through issues and reissues (the owner token
              must be spent and comes back as change) and through a transfer only when it goes back
              to one of that tx's own input addresses. Any other transfer ends the path: whoever
              got the token is not assumed to be the same person.
* ``cospend`` the address was spent in one transaction together with an address that has one of
              the proofs above (common-input ownership). Only single-party transactions count:
              every input signed SIGHASH_ALL, no Launch memo (``XL1``/``XB1``), no input from a
              Launch platform address, and no equal-output mixing shape. A cluster that touches
              two different handles gets no ``cospend`` labels at all.

A plain recipient never inherits a handle. Platform addresses (treasury, reserves, fee and
escrow keys, and every address of a platform handle) never get a user handle.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from typing import Callable, Iterable

PLATFORM_HANDLES = frozenset({"launch_xfer"})
PROOF_RANK = {"claim": 0, "wallet": 1, "owner": 2, "cospend": 3}
LAUNCH_MEMO_PREFIXES = ("584c317c", "5842317c")  # "XL1|", "XB1|"
SIGHASH_ALL = "01"
MAX_BATCH = 200
REFRESH_SECONDS = 300.0
MAX_BLOCK_READS = 400  # per rebuild; blocks not read yet are read on the next refresh

LINKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS address_links (
    address TEXT NOT NULL,
    handle TEXT NOT NULL,
    proof TEXT NOT NULL,
    detail TEXT,
    txid TEXT,
    PRIMARY KEY (address, handle, proof)
);
CREATE INDEX IF NOT EXISTS idx_links_handle ON address_links(handle);
CREATE TABLE IF NOT EXISTS tx_sighash (
    txid TEXT PRIMARY KEY,
    all_sighash_all INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_links (
    address TEXT NOT NULL,
    handle TEXT NOT NULL,
    at INTEGER,
    PRIMARY KEY (address, handle)
);
"""


def _norm(handle) -> str:
    return str(handle or "").strip().lower().lstrip("@")


def _root_of(asset: str) -> str:
    """``EVILRA99/XFERSTICKERS!`` -> ``EVILRA99``."""
    name = asset.rstrip("!")
    for sep in ("/", "#", "~", "$"):
        name = name.split(sep, 1)[0]
    return name


def sighash_flags(script_sig_hex: str | None) -> str | None:
    """Hash type byte of the first push in a scriptSig (the signature), as hex."""
    try:
        b = bytes.fromhex(script_sig_hex or "")
    except ValueError:
        return None
    if not b:
        return None
    n = b[0]
    if 1 <= n <= 75:
        sig = b[1 : 1 + n]
    elif n == 76 and len(b) > 1:
        sig = b[2 : 2 + b[1]]
    else:
        return None
    return f"{sig[-1]:02x}" if sig else None


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, a: str) -> str:
        self.parent.setdefault(a, a)
        root = a
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[a] != root:
            self.parent[a], a = root, self.parent[a]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


class AddressLinks:
    def __init__(
        self,
        db,
        rpc=None,
        platform: Iterable[str] = (),
        relay_url: str = "",
        fetch_json: Callable[[str], dict | None] | None = None,
    ):
        self.db = db
        self.rpc = rpc
        self.static_platform = {a for a in platform if a}
        self.relay_url = relay_url.rstrip("/")
        self._fetch = fetch_json or _http_json
        self._built_height = -2
        self._built_at = 0.0
        self._lock = threading.Lock()
        self._block_reads = MAX_BLOCK_READS
        self.db.conn.executescript(LINKS_SCHEMA)
        self.db.commit()

    # ---------- inputs ----------

    def _relay(self, path: str) -> dict | None:
        if not self.relay_url:
            return None
        try:
            return self._fetch(self.relay_url + path)
        except Exception:
            return None

    def _sync_wallet_links(self) -> None:
        data = self._relay("/api/identity/wallet-links")
        if not isinstance(data, dict) or not isinstance(data.get("links"), list):
            return  # keep the last good copy
        rows = []
        for item in data["links"]:
            if not isinstance(item, dict):
                continue
            addr, handle = str(item.get("address") or ""), _norm(item.get("handle"))
            if addr.startswith("X") and handle:
                rows.append((addr, handle, int(item.get("at") or 0)))
        self.db.conn.execute("DELETE FROM wallet_links")
        self.db.conn.executemany("INSERT OR IGNORE INTO wallet_links(address, handle, at) VALUES(?,?,?)", rows)

    def _platform(self) -> set[str]:
        out = set(self.static_platform)
        for table in ("launch_reserves", "launch_fee_addresses"):
            out |= {r["address"] for r in self.db.conn.execute(f"SELECT address FROM {table}").fetchall()}
        for r in self.db.conn.execute("SELECT handle, address FROM identities WHERE address IS NOT NULL").fetchall():
            if _norm(r["handle"]) in PLATFORM_HANDLES:
                out.add(r["address"])
        data = self._relay("/api/launch/platform-addresses")
        if isinstance(data, dict):
            out |= {str(a) for a in data.get("addresses") or [] if str(a).startswith("X")}
        out.discard(None)
        return out

    def remember_block(self, txs: Iterable[dict]) -> None:
        """Cache the sighash check for every spending tx of a ``getblock <hash> 2`` reply."""
        rows = []
        for tx in txs:
            vin = tx.get("vin") if isinstance(tx, dict) else None
            if not vin or any("coinbase" in v for v in vin) or not tx.get("txid"):
                continue
            flags = [sighash_flags((v.get("scriptSig") or {}).get("hex")) for v in vin]
            rows.append((tx["txid"], 1 if all(f == SIGHASH_ALL for f in flags) else 0))
        self.db.conn.executemany("INSERT OR REPLACE INTO tx_sighash(txid, all_sighash_all) VALUES(?,?)", rows)

    def _all_sighash_all(self, txid: str) -> bool | None:
        row = self.db.conn.execute("SELECT all_sighash_all FROM tx_sighash WHERE txid=?", (txid,)).fetchone()
        if row is not None:
            return bool(row["all_sighash_all"])
        if self.rpc is None or not getattr(self.rpc, "connected", False) or self._block_reads <= 0:
            return None
        # Read the whole block: works without txindex, and caches every tx in it at once.
        tx_row = self.db.conn.execute("SELECT height FROM txs WHERE txid=?", (txid,)).fetchone()
        if tx_row is None or tx_row["height"] is None:
            return None
        self._block_reads -= 1
        block_hash = self.rpc.try_call("getblockhash", int(tx_row["height"]), default=None)
        block = self.rpc.try_call("getblock", block_hash, 2, default=None) if block_hash else None
        if not isinstance(block, dict):
            return None
        self.remember_block(block.get("tx") or [])
        row = self.db.conn.execute("SELECT all_sighash_all FROM tx_sighash WHERE txid=?", (txid,)).fetchone()
        return None if row is None else bool(row["all_sighash_all"])

    # ---------- build ----------

    def maybe_refresh(self, indexed_height: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if indexed_height == self._built_height and now - self._built_at < REFRESH_SECONDS:
            return False
        self.rebuild()
        self._built_height = indexed_height
        self._built_at = now
        return True

    def rebuild(self) -> dict:
        with self._lock:
            self._block_reads = MAX_BLOCK_READS
            self._sync_wallet_links()
            platform = self._platform()
            links: dict[tuple[str, str, str], tuple[str, str | None]] = {}

            def add(addr, handle, proof, detail, txid=None):
                handle = _norm(handle)
                if not addr or not handle:
                    return
                if addr in platform and handle not in PLATFORM_HANDLES and proof != "claim":
                    return
                links.setdefault((addr, handle, proof), (detail, txid))

            claimed: dict[str, str] = {}
            for r in self.db.conn.execute("SELECT handle, asset, address, txid FROM identities").fetchall():
                if r["address"]:
                    add(r["address"], r["handle"], "claim", f"holds {r['asset']}" if r["asset"] else "signed-in node", r["txid"])
                if r["asset"]:
                    claimed[str(r["asset"]).upper()] = _norm(r["handle"])

            for r in self.db.conn.execute("SELECT address, handle FROM wallet_links").fetchall():
                add(r["address"], r["handle"], "wallet", "signed by this handle's Launch vault")

            self._owner_paths(claimed, add)

            for (addr, handle, _p) in list(links):
                if handle in PLATFORM_HANDLES:
                    platform.add(addr)
            cospend = self._cospend(links, platform, add)

            self.db.conn.execute("DELETE FROM address_links")
            self.db.conn.executemany(
                "INSERT INTO address_links(address, handle, proof, detail, txid) VALUES(?,?,?,?,?)",
                [(a, h, p, d, t) for (a, h, p), (d, t) in links.items()],
            )
            self.db.commit()
            counts = Counter(p for (_a, _h, p) in links)
            return {"links": len(links), **counts, "clusters_dropped": cospend}

    def _owner_paths(self, claimed: dict[str, str], add) -> None:
        """Follow each claimed owner token from its creation, in chain order."""
        if not claimed:
            return
        rows = self.db.conn.execute(
            """
            SELECT x.txid, x.n, x.direction, x.address, x.asset, x.asset_kind, x.spent_txid, x.spent_n
            FROM txio x JOIN txs t ON t.txid = x.txid
            WHERE x.asset LIKE '%!'
            ORDER BY t.height, t.n, x.n
            """
        ).fetchall()
        by_tx: dict[str, list] = defaultdict(list)
        order: list[str] = []
        for r in rows:
            if _root_of(r["asset"]) not in claimed:
                continue
            if r["txid"] not in by_tx:
                order.append(r["txid"])
            by_tx[r["txid"]].append(r)

        trusted: dict[tuple[str, int], str] = {}  # owner-token outpoint -> handle
        for txid in order:
            ios = by_tx[txid]
            ins = [r for r in ios if r["direction"] == "in"]
            outs = [r for r in ios if r["direction"] == "out"]
            spent = [(r, trusted.pop((r["spent_txid"], r["spent_n"]), None)) for r in ins]
            spent_handles = {h for _r, h in spent if h}
            handle = next(iter(spent_handles)) if len(spent_handles) == 1 else None
            for r, h in spent:
                if h:
                    add(r["address"], h, "owner", f"signed with {r['asset']}", txid)
            new_kinds = {r["asset_kind"] for r in outs}
            if handle is None:
                # The very first output of a claimed root's owner token starts the path.
                for r in outs:
                    if r["asset_kind"] == "new" and r["asset"].rstrip("!") in claimed and r["asset"].count("/") == 0:
                        h = claimed[r["asset"].rstrip("!")]
                        trusted[(txid, r["n"])] = h
                        add(r["address"], h, "owner", f"received {r['asset']} when it was created", txid)
                continue
            input_addrs = self._input_addresses(txid)
            spent_names = {r["asset"] for r, h in spent if h}
            issue = bool(new_kinds & {"new", "reissue"}) or self._issues(txid)
            for r in outs:
                same_token = r["asset"] in spent_names
                if (issue and same_token) or r["address"] in input_addrs:
                    trusted[(txid, r["n"])] = handle
                    add(r["address"], handle, "owner", f"holds {r['asset']}", txid)

    def _input_addresses(self, txid: str) -> set[str]:
        return {
            r["address"]
            for r in self.db.conn.execute(
                "SELECT address FROM txio WHERE txid=? AND direction='in' AND address IS NOT NULL", (txid,)
            ).fetchall()
        }

    def _issues(self, txid: str) -> bool:
        row = self.db.conn.execute(
            "SELECT 1 FROM txio WHERE txid=? AND direction='out' AND asset_kind IN ('new','reissue') LIMIT 1", (txid,)
        ).fetchone()
        return row is not None

    def _cospend(self, links: dict, platform: set[str], add) -> int:
        multi = self.db.conn.execute(
            """
            SELECT txid, GROUP_CONCAT(DISTINCT address) AS addrs, COUNT(DISTINCT address) AS k
            FROM txio WHERE direction='in' AND coinbase=0 AND address IS NOT NULL
            GROUP BY txid HAVING k > 1
            """
        ).fetchall()
        uf = _UnionFind()
        via: dict[str, str] = {}
        for r in multi:
            addrs = sorted(set(r["addrs"].split(",")))
            if not self._single_party(r["txid"], addrs, platform):
                continue
            for a in addrs[1:]:
                uf.union(addrs[0], a)
            for a in addrs:
                via.setdefault(a, r["txid"])

        anchors: dict[str, set[str]] = defaultdict(set)
        for (addr, handle, _p) in links:
            if addr in uf.parent:
                root = uf.find(addr)
                anchors[root].add(handle)
        linked = {a for (a, _h, _p) in links}
        members: dict[str, list[str]] = defaultdict(list)
        for addr in list(uf.parent):
            members[uf.find(addr)].append(addr)
        dropped = 0
        for root, handles in anchors.items():
            if len(handles) != 1:
                dropped += 1
                continue
            handle = next(iter(handles))
            if handle in PLATFORM_HANDLES:
                continue
            for addr in members[root]:
                if addr in platform or addr in linked:
                    continue
                add(addr, handle, "cospend", f"spent in one transaction with other @{handle} addresses", via.get(addr))
        return dropped

    def _single_party(self, txid: str, addrs: list[str], platform: set[str]) -> bool:
        if any(a in platform for a in addrs):
            return False
        outs = self.db.conn.execute(
            "SELECT address, value, op_return FROM txio WHERE txid=? AND direction='out'", (txid,)
        ).fetchall()
        for o in outs:
            memo = (o["op_return"] or "").lower()
            if memo.startswith(LAUNCH_MEMO_PREFIXES):
                return False
        if len(addrs) >= 3:
            values = Counter(o["value"] for o in outs if o["value"] and o["address"])
            if values and values.most_common(1)[0][1] >= 3:
                return False
        return self._all_sighash_all(txid) is True

    # ---------- reads ----------

    def labels(self, addresses: Iterable[str]) -> dict[str, list[dict]]:
        """Address -> links, strongest proof first. Unlinked addresses are left out."""
        addrs = sorted({a for a in addresses if isinstance(a, str) and a.startswith("X")})[: MAX_BATCH * 5]
        out: dict[str, list[dict]] = {}
        for i in range(0, len(addrs), MAX_BATCH):
            chunk = addrs[i : i + MAX_BATCH]
            marks = ",".join("?" * len(chunk))
            for r in self.db.conn.execute(
                f"SELECT address, handle, proof, detail, txid FROM address_links WHERE address IN ({marks})", chunk
            ).fetchall():
                out.setdefault(r["address"], []).append(
                    {"handle": r["handle"], "proof": r["proof"], "detail": r["detail"], "txid": r["txid"]}
                )
        for rows in out.values():
            rows.sort(key=lambda x: (PROOF_RANK.get(x["proof"], 9), x["handle"]))
            # One entry per handle: its strongest proof.
            seen: set[str] = set()
            rows[:] = [x for x in rows if not (x["handle"] in seen or seen.add(x["handle"]))]
        return out


def _http_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "xcoin-explorer"})
    with urllib.request.urlopen(req, timeout=4) as res:  # noqa: S310 - fixed local relay URL
        return json.loads(res.read(2_000_000).decode("utf-8"))


def addresses_in(obj, limit: int = 1000) -> set[str]:
    """Every X… address string under ``address``-like keys of a response."""
    found: set[str] = set()
    keys = ("address", "issuer", "host_address", "trader", "creator")

    def walk(o):
        if len(found) >= limit:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k in keys and isinstance(v, str) and v.startswith("X"):
                    found.add(v)
                elif isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return found
