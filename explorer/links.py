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
              two different handles gets no ``cospend`` labels at all. The same proof is used for
              a new change output of that wallet: the leftover address when the payment went to
              an address already on chain or to a platform address. On a Launch trade the
              reserve, fee, and escrow stay the payment; only the leftover output is change.

A plain recipient never inherits a handle. Platform addresses (treasury, reserves, fee and
escrow keys, the relay's platform list, and every address of a platform handle) never get
a user handle. Until that relay list has loaded once, a change output gets no handle: it
might be a platform address this process has not seen yet. A failed relay call keeps the
previous list.

``address_links`` is a snapshot. The indexer rebuilds it when new blocks land (only the
new transactions, when the relay list is already loaded) and in full on a slow timer so
wallet-confirmations and owner-token paths are picked up. A page read does not walk the
chain. A mempool transaction is applied with the same rules when a page asks about it, so
a change address in that tx resolves before the tx is in a block.
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


def _is_launch_memo(value: str | None) -> bool:
    """True for an ``XL1`` / ``XB1`` payload stored as text or as hex."""
    memo = (value or "").strip().lower()
    if not memo:
        return False
    if memo.startswith(LAUNCH_MEMO_PREFIXES) or memo.startswith(("xl1|", "xb1|")):
        return True
    return False


def tx_sighash_all(raw: dict | None) -> bool | None:
    """True when every input of a verbose tx signs SIGHASH_ALL. None if a signature is missing."""
    vins = raw.get("vin") if isinstance(raw, dict) else None
    if not vins or any(isinstance(v, dict) and "coinbase" in v for v in vins):
        return None
    flags: list[str | None] = []
    for vin in vins:
        if not isinstance(vin, dict):
            return None
        flags.append(sighash_flags((vin.get("scriptSig") or {}).get("hex")))
    if any(flag is None for flag in flags):
        return None
    return all(flag == SIGHASH_ALL for flag in flags)


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
        self._dirty = True
        self._generation = 0
        self._platform_set: set[str] = set(self.static_platform)
        # Last platform list the relay actually returned. Independent of _platform_set
        # so a failed or skipped fetch cannot replace it with a smaller set.
        self._relay_platform: set[str] = set()
        # No relay configured: there is no list to wait for, and change labels are allowed.
        self._relay_platform_ok = not bool(self.relay_url)
        self._lock = threading.RLock()
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

    def _refresh_relay_platform(self) -> bool:
        """Load ``/api/launch/platform-addresses``. Failure keeps the previous list.

        A body of ``{"addresses": [...]}`` is success, including an empty list (that
        list is the source of truth). ``None``, a non-object, or a missing / non-list
        ``addresses`` field is failure and does not change what was saved.
        """
        if not self.relay_url:
            self._relay_platform_ok = True
            return True
        data = self._relay("/api/launch/platform-addresses")
        if not isinstance(data, dict) or not isinstance(data.get("addresses"), list):
            return False
        self._relay_platform = {str(a) for a in data["addresses"] if str(a).startswith("X")}
        self._relay_platform_ok = True
        return True

    def _platform(self) -> set[str]:
        """Static proceeds, the last good relay list, and platform rows in the index.

        Does not call the relay. Call ``_refresh_relay_platform`` first when a fresh
        list is wanted.
        """
        out = set(self.static_platform)
        out |= set(self._relay_platform)
        for table in ("launch_reserves", "launch_fee_addresses"):
            out |= {r["address"] for r in self.db.conn.execute(f"SELECT address FROM {table}").fetchall()}
        for r in self.db.conn.execute("SELECT handle, address FROM identities WHERE address IS NOT NULL").fetchall():
            if _norm(r["handle"]) in PLATFORM_HANDLES and r["address"]:
                out.add(r["address"])
        out.discard(None)
        out.discard("")
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

    def invalidate(self) -> None:
        """The index was rewritten at a height already built. The indexer does a full rebuild."""
        with self._lock:
            self._dirty = True

    @property
    def generation(self) -> int:
        return self._generation

    def stale(self) -> bool:
        """True when ``address_links`` was built for an older chain, or never built."""
        with self._lock:
            dirty = self._dirty
            built = self._built_height
        if dirty or built < -1:
            return True
        return self.db.indexed_height() != built

    def ensure_fresh(self) -> bool:
        """Make sure one snapshot exists and the relay platform list has loaded.

        A page does not rescan the chain when the tip moves. That work belongs to
        the indexer. The first build, and the first build after the relay list
        finally loads, still walk the chain so change labels can use the list.
        While the list has never loaded and a snapshot is already stored, only
        the relay call is retried.
        """
        if self._built_height >= -1 and self._relay_platform_ok:
            return False
        if self._built_height >= -1:
            if not self._refresh_relay_platform():
                return False
            self.rebuild(sync_wallets=False, force=True)
            return True
        self.rebuild(sync_wallets=False)
        return True

    def maybe_refresh(self, indexed_height: int, now: float | None = None) -> bool:
        return self.refresh_index(indexed_height, now)

    def refresh_index(self, indexed_height: int, now: float | None = None) -> bool:
        """Indexer thread. Full rebuild, or only the transactions above the last build.

        Full rebuild when there is no snapshot yet, the relay platform list has
        never loaded, the chain rewound, the slow timer is due (wallet links and
        owner-token paths), or the same height was rewritten. Otherwise the new
        block's change and co-spend inputs are absorbed without walking history.
        """
        now = time.time() if now is None else now
        with self._lock:
            built = self._built_height
            dirty = self._dirty
            built_at = self._built_at
            ok = self._relay_platform_ok
        full = (
            built < -1
            or not ok
            or indexed_height < built
            or now - built_at >= REFRESH_SECONDS
            or (dirty and indexed_height <= built)
        )
        if full:
            self.rebuild(sync_wallets=True)
            return True
        if indexed_height > built:
            self._absorb_new(built)
            return True
        return False

    def rebuild(self, *, sync_wallets: bool = True, force: bool = False) -> dict:
        with self._lock:
            height = self.db.indexed_height()
            if (
                not force
                and not sync_wallets
                and not self._dirty
                and self._built_height >= -1
                and height == self._built_height
                and self._relay_platform_ok
            ):
                return {"links": 0, "skipped": True}
            self._block_reads = MAX_BLOCK_READS
            self._ctx = None
            if sync_wallets:
                self._sync_wallet_links()
            # Always try. A failure leaves _relay_platform as it was.
            self._refresh_relay_platform()
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
            # Change outputs are anchors for a later spend of that change.
            # Co-inputs are resolved again afterwards so a fresh address spent
            # with that change is labeled in the same build.
            self._cospend(links, platform, add)
            self._change_outputs(links, platform, add)
            cospend = self._cospend(links, platform, add)

            self.db.conn.execute("DELETE FROM address_links")
            self.db.conn.executemany(
                "INSERT INTO address_links(address, handle, proof, detail, txid) VALUES(?,?,?,?,?)",
                [(a, h, p, d, t) for (a, h, p), (d, t) in links.items()],
            )
            self.db.commit()
            self._platform_set = set(platform)
            self._dirty = False
            self._built_height = self.db.indexed_height()
            self._built_at = time.time()
            self._generation += 1
            counts = Counter(p for (_a, _h, p) in links)
            return {"links": len(links), **counts, "clusters_dropped": cospend}

    def _absorb_new(self, after_height: int) -> dict:
        """Change and co-spend labels for transactions above ``after_height``.

        Does not reset ``_built_at``: the slow timer still owes a full rebuild
        for owner-token paths and wallet-link polling.
        """
        with self._lock:
            platform = self._platform()
            platform |= set(self._platform_set)
            by_addr: dict[str, set[str]] = defaultdict(set)
            for row in self.db.conn.execute("SELECT address, handle FROM address_links").fetchall():
                if row["address"] and row["handle"]:
                    by_addr[row["address"]].add(row["handle"])
                    if row["handle"] in PLATFORM_HANDLES:
                        platform.add(row["address"])

            rows = self.db.conn.execute(
                """
                SELECT t.txid AS txid, x.direction AS direction, x.n AS n, x.address AS address,
                       x.value AS value, x.asset AS asset, x.asset_amount AS asset_amount,
                       x.asset_kind AS asset_kind, x.op_return AS op_return
                FROM txs t
                JOIN txio x ON x.txid = t.txid
                WHERE IFNULL(t.coinbase, 0) = 0 AND t.height > ?
                ORDER BY t.height, t.n, CASE x.direction WHEN 'in' THEN 0 ELSE 1 END, x.n
                """,
                (after_height,),
            ).fetchall()
            order: list[str] = []
            grouped: dict[str, dict] = {}
            out_addrs: list[str] = []
            for r in rows:
                tx = grouped.get(r["txid"])
                if tx is None:
                    tx = {"txid": r["txid"], "vin": [], "vout": []}
                    grouped[r["txid"]] = tx
                    order.append(r["txid"])
                leg = {
                    "address": r["address"],
                    "value": int(r["value"] or 0),
                    "asset": r["asset"],
                    "asset_amount": int(r["asset_amount"] or 0),
                    "asset_kind": r["asset_kind"],
                    "op_return": r["op_return"],
                    "n": r["n"],
                }
                tx["vin" if r["direction"] == "in" else "vout"].append(leg)
                if r["direction"] == "out" and r["address"]:
                    out_addrs.append(r["address"])

            seen: set[str] = set()
            if after_height >= 0 and out_addrs:
                wanted = list(dict.fromkeys(out_addrs))
                for i in range(0, len(wanted), MAX_BATCH):
                    chunk = wanted[i : i + MAX_BATCH]
                    marks = ",".join("?" * len(chunk))
                    for row in self.db.conn.execute(
                        f"""
                        SELECT DISTINCT x.address AS address
                        FROM txio x JOIN txs t ON t.txid = x.txid
                        WHERE t.height <= ? AND x.address IN ({marks})
                        """,
                        (after_height, *chunk),
                    ).fetchall():
                        if row["address"]:
                            seen.add(row["address"])

            sighash = {
                r["txid"]: bool(r["all_sighash_all"])
                for r in self.db.conn.execute("SELECT txid, all_sighash_all FROM tx_sighash").fetchall()
            }
            new: list[tuple] = []

            def add(addr, handle, proof, detail, txid=None):
                handle = _norm(handle)
                if not addr or not handle:
                    return
                if addr in platform and handle not in PLATFORM_HANDLES and proof != "claim":
                    return
                if handle in by_addr.get(addr, ()):
                    return
                by_addr.setdefault(addr, set()).add(handle)
                new.append((addr, handle, proof, detail, txid))

            ctx = self._trade_context()
            for txid in order:
                self._attribute_tx(
                    grouped[txid], by_addr, platform, seen, add, sighash.get(txid), ctx, link_inputs=True
                )
            if new:
                self.db.conn.executemany(
                    "INSERT OR IGNORE INTO address_links(address, handle, proof, detail, txid) VALUES(?,?,?,?,?)",
                    new,
                )
            self.db.commit()
            self._platform_set |= platform
            self._dirty = False
            self._built_height = self.db.indexed_height()
            self._generation += 1
            self._ctx = None
            return {"links": len(new)}

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
            if _is_launch_memo(o["op_return"]):
                return False
        if len(addrs) >= 3:
            values = Counter(o["value"] for o in outs if o["value"] and o["address"])
            if values and values.most_common(1)[0][1] >= 3:
                return False
        return self._all_sighash_all(txid) is True

    # ---------- change outputs, and one mempool tx ----------

    def _handles_of(self, by_addr: dict[str, set[str]], addr: str) -> set[str]:
        return {h for h in by_addr.get(addr, ()) if h not in PLATFORM_HANDLES}

    def _change_outputs(self, links: dict, platform: set[str], add) -> None:
        """Leftover outputs of a wallet, in chain order. Does not relabel co-inputs.

        Co-inputs are already decided by ``_cospend`` (including clusters that
        touch two handles). Doing that again here would put those links back.
        """
        by_addr: dict[str, set[str]] = defaultdict(set)
        for addr, handle, _proof in links:
            by_addr[addr].add(handle)

        def add_change(addr, handle, proof, detail, txid=None):
            add(addr, handle, proof, detail, txid)
            if any(a == addr and h == handle for (a, h, _p) in links):
                by_addr[addr].add(handle)

        sighash = {
            r["txid"]: bool(r["all_sighash_all"])
            for r in self.db.conn.execute("SELECT txid, all_sighash_all FROM tx_sighash").fetchall()
        }
        rows = self.db.conn.execute(
            """
            SELECT t.txid AS txid, x.direction AS direction, x.n AS n, x.address AS address,
                   x.value AS value, x.asset AS asset, x.asset_amount AS asset_amount,
                   x.asset_kind AS asset_kind, x.op_return AS op_return
            FROM txs t
            JOIN txio x ON x.txid = t.txid
            WHERE IFNULL(t.coinbase, 0) = 0
            ORDER BY t.height, t.n, CASE x.direction WHEN 'in' THEN 0 ELSE 1 END, x.n
            """
        ).fetchall()
        order: list[str] = []
        grouped: dict[str, dict] = {}
        for r in rows:
            tx = grouped.get(r["txid"])
            if tx is None:
                tx = {"txid": r["txid"], "vin": [], "vout": []}
                grouped[r["txid"]] = tx
                order.append(r["txid"])
            leg = {
                "address": r["address"],
                "value": int(r["value"] or 0),
                "asset": r["asset"],
                "asset_amount": int(r["asset_amount"] or 0),
                "asset_kind": r["asset_kind"],
                "op_return": r["op_return"],
                "n": r["n"],
            }
            tx["vin" if r["direction"] == "in" else "vout"].append(leg)
        seen: set[str] = set()
        ctx = self._trade_context()
        for txid in order:
            self._attribute_tx(
                grouped[txid],
                by_addr,
                platform,
                seen,
                add_change,
                sighash.get(txid),
                ctx,
                link_inputs=False,
            )

    def _trade_context(self) -> dict:
        cached = getattr(self, "_ctx", None)
        if cached is not None and getattr(self, "_ctx_gen", None) == self._generation:
            return cached
        units: dict[str, int] = {}
        reserves: dict[str, str] = {}
        fees: list[str] = []
        for row in self.db.conn.execute("SELECT name, units FROM assets").fetchall():
            if row["name"] and int(row["units"] or 0) > 0:
                units[row["name"]] = int(row["units"])
        for row in self.db.conn.execute("SELECT asset, address FROM launch_reserves").fetchall():
            if row["asset"] and row["address"]:
                reserves[row["asset"]] = row["address"]
        for row in self.db.conn.execute("SELECT address FROM launch_fee_addresses").fetchall():
            if row["address"]:
                fees.append(row["address"])
        ctx = {"units": units, "reserves": reserves, "fees": fees}
        self._ctx = ctx
        self._ctx_gen = self._generation
        return ctx

    def _attribute_tx(
        self,
        tx: dict,
        by_addr: dict[str, set[str]],
        platform: set[str],
        seen: set[str],
        add,
        sighash_all: bool | None,
        ctx: dict,
        *,
        link_inputs: bool,
    ) -> None:
        """One transaction. ``link_inputs`` is for a tx that is not in the snapshot yet."""
        ins = [row.get("address") for row in tx.get("vin") or [] if row.get("address")]
        outs = list(tx.get("vout") or [])

        def see():
            for row in list(tx.get("vin") or []) + outs:
                addr = row.get("address")
                if addr:
                    seen.add(addr)

        if sighash_all is not True or self._is_mixing(ins, outs):
            see()
            return
        user_ins = [addr for addr in ins if addr not in platform]
        handles: set[str] = set()
        for addr in user_ins:
            handles |= self._handles_of(by_addr, addr)
        if len(handles) != 1:
            see()
            return
        handle = next(iter(handles))
        memo = any(_is_launch_memo(row.get("op_return")) for row in outs)
        platform_in = any(addr in platform for addr in ins)
        if link_inputs and not memo and not platform_in:
            for addr in user_ins:
                if self._handles_of(by_addr, addr):
                    continue
                add(
                    addr,
                    handle,
                    "cospend",
                    f"spent in one transaction with other @{handle} addresses",
                    tx.get("txid"),
                )
                by_addr[addr].add(handle)
        if memo or platform_in:
            self._link_trade_change(tx, handle, by_addr, platform, seen, add, ctx)
        else:
            self._link_plain_change(tx, handle, user_ins, by_addr, platform, seen, add)
        see()

    def _is_mixing(self, addrs: list[str], outs: list[dict]) -> bool:
        if len(set(addrs)) < 3:
            return False
        values = Counter(int(row.get("value") or 0) for row in outs if row.get("value") and row.get("address"))
        return bool(values and values.most_common(1)[0][1] >= 3)

    def _link_plain_change(self, tx, handle, user_ins, by_addr, platform, seen, add) -> None:
        """The one new output is change when every other payment is already known."""
        if not self._relay_platform_ok:
            return
        inputs = set(user_ins)
        fresh: list[str] = []
        explicit = False
        payment = False
        for row in tx.get("vout") or []:
            addr = row.get("address")
            if not addr:
                continue
            if addr in platform:
                payment = True
                continue
            if addr in inputs or handle in self._handles_of(by_addr, addr):
                explicit = True
                continue
            if addr in seen:
                payment = True
                continue
            fresh.append(addr)
        if explicit or not payment or len(set(fresh)) != 1:
            return
        add(fresh[0], handle, "cospend", f"change from a transaction signed by @{handle}", tx.get("txid"))

    def _link_trade_change(self, tx, handle, by_addr, platform, seen, add, ctx) -> None:
        """Change on a Launch fill. Reserve, fee, and escrow stay unlabeled."""
        if not self._relay_platform_ok:
            return
        from explorer.trades import classify_launch_trade

        proceeds = tuple(self.static_platform) if self.static_platform else None
        try:
            trade = classify_launch_trade(
                tx,
                proceeds,
                asset_units=ctx.get("units"),
                reserves=ctx.get("reserves"),
                fee_addresses=ctx.get("fees"),
            )
        except Exception:
            return
        if not trade:
            return
        trader = trade.get("trader") or ""
        if handle not in self._handles_of(by_addr, trader):
            return
        skip = set(platform)
        skip.add(trader)
        for key in ("counterparty", "reserve", "fee_address"):
            if trade.get(key):
                skip.add(trade[key])
        fresh: list[str] = []
        for row in tx.get("vout") or []:
            addr = row.get("address")
            if not addr or addr in skip or addr in platform:
                continue
            if row.get("asset") or _is_launch_memo(row.get("op_return")) or not row.get("value"):
                continue
            theirs = self._handles_of(by_addr, addr)
            if theirs and handle not in theirs:
                continue
            if addr in seen and handle not in theirs:
                continue
            fresh.append(addr)
        uniq = list(dict.fromkeys(fresh))
        if len(uniq) != 1:
            return
        add(uniq[0], handle, "cospend", f"change from a transaction signed by @{handle}", tx.get("txid"))

    def observe_tx(self, tx: dict) -> None:
        """Apply the same rules to one tx that is not in the snapshot (a mempool tx)."""
        if not isinstance(tx, dict) or not tx.get("txid"):
            return
        self.ensure_fresh()
        with self._lock:
            sighash = tx.get("sighash_all")
            if sighash is None:
                row = self.db.conn.execute(
                    "SELECT all_sighash_all FROM tx_sighash WHERE txid=?", (tx["txid"],)
                ).fetchone()
                sighash = None if row is None else bool(row["all_sighash_all"])
            addrs = [
                row.get("address")
                for row in list(tx.get("vin") or []) + list(tx.get("vout") or [])
                if isinstance(row, dict) and row.get("address")
            ]
            by_addr = self._load_handles(addrs)
            seen = self._seen_in_index([row.get("address") for row in tx.get("vout") or [] if isinstance(row, dict)])
            platform = self._platform()
            platform |= set(self._platform_set)
            new: list[tuple] = []

            def add(addr, handle, proof, detail, txid=None):
                handle = _norm(handle)
                if not addr or not handle:
                    return
                if addr in platform and handle not in PLATFORM_HANDLES and proof != "claim":
                    return
                if handle in by_addr.get(addr, ()):
                    return
                by_addr.setdefault(addr, set()).add(handle)
                new.append((addr, handle, proof, detail, txid or tx.get("txid")))

            self._attribute_tx(
                tx, by_addr, platform, seen, add, sighash, self._trade_context(), link_inputs=True
            )
            if not new:
                return
            self.db.conn.executemany(
                "INSERT OR IGNORE INTO address_links(address, handle, proof, detail, txid) VALUES(?,?,?,?,?)",
                new,
            )
            self.db.commit()

    def _load_handles(self, addrs: list[str]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        wanted = [a for a in dict.fromkeys(addrs) if isinstance(a, str) and a.startswith("X")]
        for i in range(0, len(wanted), MAX_BATCH):
            chunk = wanted[i : i + MAX_BATCH]
            marks = ",".join("?" * len(chunk))
            for row in self.db.conn.execute(
                f"SELECT address, handle FROM address_links WHERE address IN ({marks})", chunk
            ).fetchall():
                out[row["address"]].add(row["handle"])
        return out

    def _seen_in_index(self, addrs: list[str]) -> set[str]:
        wanted = [a for a in dict.fromkeys(addrs) if isinstance(a, str) and a.startswith("X")]
        found: set[str] = set()
        for i in range(0, len(wanted), MAX_BATCH):
            chunk = wanted[i : i + MAX_BATCH]
            marks = ",".join("?" * len(chunk))
            for row in self.db.conn.execute(
                f"SELECT DISTINCT address FROM txio WHERE address IN ({marks})", chunk
            ).fetchall():
                if row["address"]:
                    found.add(row["address"])
        return found

    # ---------- reads ----------

    def labels(self, addresses: Iterable[str], txs: Iterable[dict] | None = None) -> dict[str, list[dict]]:
        """Address -> links, strongest proof first. Unlinked addresses are left out.

        ``txs`` are mempool (or otherwise unindexed) transactions. They are
        resolved with the same rules as a confirmed tx and then stored, so the
        address page and the tx page agree.
        """
        self.ensure_fresh()
        for tx in txs or ():
            if isinstance(tx, dict):
                self.observe_tx(tx)
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
    lists = ("addresses", "parties")

    def walk(o):
        if len(found) >= limit:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k in keys and isinstance(v, str) and v.startswith("X"):
                    found.add(v)
                elif k in lists and isinstance(v, list):
                    for item in v:
                        if isinstance(item, str) and item.startswith("X"):
                            found.add(item)
                        elif isinstance(item, (dict, list)):
                            walk(item)
                elif isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return found
