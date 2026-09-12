"""Pull blocks from xcoind and fill the local SQLite index."""

from __future__ import annotations

import threading
import time
from typing import Any

from explorer.amounts import xfer_to_atoms
from explorer.chain import (
    classify_asset_name,
    halving_interval_for_network,
    slot_from_height,
    subsidy_at,
)
from explorer.db import Database
from explorer.decode import (
    lottery_node_id_from_script_hex,
    parse_vout_script,
    seed_for_draw,
    select_winners,
)
from explorer.rpc import RpcError, XCoinRPC


def _atoms(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return xfer_to_atoms(value)


class Indexer:
    def __init__(self, db: Database, rpc: XCoinRPC, batch_size: int = 40, poll_seconds: float = 4.0):
        self.db = db
        self.rpc = rpc
        self.batch_size = batch_size
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status: dict[str, Any] = {
            "running": False,
            "indexing": False,
            "tip": -1,
            "indexed": -1,
            "error": "",
            "network": "",
        }
        self._live_nodes: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._repair_genesis()
        self.db.commit()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="xcoin-indexer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)

    def live_nodes(self) -> list[dict]:
        with self._lock:
            return list(self._live_nodes.values())

    def _loop(self) -> None:
        self.status["running"] = True
        while not self._stop.is_set():
            try:
                if not self.rpc.connected:
                    self.rpc.connect()
                if self.rpc.connected:
                    self.tick()
                else:
                    self.status["error"] = self.rpc.last_error
                    with self._lock:
                        self._live_nodes = {}
            except Exception as e:
                self.status["error"] = str(e)
            self._stop.wait(self.poll_seconds)
        self.status["running"] = False

    def tick(self) -> None:
        info = self.rpc.call("getblockchaininfo")
        tip = int(info.get("blocks") or 0)
        network = info.get("chain") or self.rpc.network or "main"
        self.status["tip"] = tip
        self.status["network"] = network
        self.db.set_meta("network", network)
        self.db.set_meta("best_hash", info.get("bestblockhash") or "")
        self._repair_genesis()
        self.db.commit()

        self._rebuild_lottery_from_xva()
        self._refresh_lottery_live()

        indexed = self.db.indexed_height()
        if indexed >= 0:
            try:
                known = self.db.conn.execute(
                    "SELECT hash FROM blocks WHERE height=?", (indexed,)
                ).fetchone()
                actual = self.rpc.call("getblockhash", indexed)
                if known and known["hash"] != actual:
                    rewind_to = max(indexed - 32, -1)
                    self._rewind(rewind_to)
                    indexed = self.db.indexed_height()
            except RpcError:
                pass

        self.status["indexing"] = indexed < tip
        start = indexed + 1
        end = min(tip, start + self.batch_size - 1)
        if start <= end:
            for height in range(start, end + 1):
                if self._stop.is_set():
                    break
                self._index_height(height, network)
            self.db.commit()
        self.status["indexed"] = self.db.indexed_height()
        self.status["error"] = ""
        self.status["indexing"] = self.status["indexed"] < tip

    def _rebuild_lottery_from_xva(self) -> None:
        """Re-read coinbases so history/leaderboard use XVA1 handles, not live peers."""
        if self.db.get_meta("lottery_v") == "3":
            return
        if not self.rpc.connected:
            return
        network = self.status.get("network") or self.db.get_meta("network") or "main"
        indexed = self.db.indexed_height()
        self.db.conn.execute("DELETE FROM lottery_wins WHERE height>=1")
        self.db.conn.execute("DELETE FROM lottery_active WHERE height>=1")
        for height in range(1, max(indexed, 0) + 1):
            if self._stop.is_set():
                return
            try:
                block_hash = self.rpc.call("getblockhash", height)
                block = self.rpc.call("getblock", block_hash, 2)
            except RpcError:
                continue
            txs = block.get("tx") or []
            if not txs:
                continue
            cb = txs[0]
            if isinstance(cb, str):
                try:
                    cb = self.rpc.call("getrawtransaction", cb, True)
                except RpcError:
                    continue
            self._write_lottery_from_coinbase(cb, height, network)
        self.db.set_meta("lottery_v", "3")
        self.db.commit()

    def _write_lottery_from_coinbase(self, tx: dict, height: int, network: str) -> None:
        """Parse XVA1 + payees on an already-indexed coinbase (rebuild path)."""
        if height < 1:
            return
        vouts = tx.get("vout") or []
        xva: dict[str, str] = {}
        self.db.conn.execute("DELETE FROM lottery_wins WHERE height=?", (height,))
        self.db.conn.execute("DELETE FROM lottery_active WHERE height=?", (height,))
        for vout in vouts:
            spk = vout.get("scriptPubKey") or {}
            hexscript = spk.get("hex") or ""
            parsed = parse_vout_script(hexscript, network) if hexscript else {}
            if parsed.get("xhb1"):
                for nid in parsed["xhb1"]:
                    self.db.conn.execute(
                        "INSERT OR IGNORE INTO lottery_active(height, node_id) VALUES(?,?)",
                        (height, nid),
                    )
            if parsed.get("xva1"):
                for row in parsed["xva1"]:
                    nid = (row.get("node_id") or "").lower()
                    handle = (row.get("handle") or "").lower().lstrip("@")
                    if nid and handle:
                        xva[nid] = handle
        rank = 0
        for vout in vouts:
            spk = vout.get("scriptPubKey") or {}
            hexscript = spk.get("hex") or ""
            parsed = parse_vout_script(hexscript, network) if hexscript else {}
            st = spk.get("type") or parsed.get("script_type")
            if st in ("nulldata", "nonstandard") or parsed.get("xhb1") or parsed.get("xva1"):
                continue
            node_id = lottery_node_id_from_script_hex(hexscript) if hexscript else None
            handle = xva.get((node_id or "").lower()) if node_id else None
            addresses = spk.get("addresses") or []
            address = addresses[0] if addresses else parsed.get("address")
            value = _atoms(vout.get("value"))
            self.db.conn.execute(
                """
                INSERT OR REPLACE INTO lottery_wins(height, rank, node_id, address, amount, xaccount, is_producer)
                VALUES(?,?,?,?,?,?,?)
                """,
                (height, rank, node_id, address, value, handle, 1 if rank == 0 else 0),
            )
            rank += 1

    def _repair_genesis(self) -> None:
        """Height 0 is not a payday and its coinbase is not a UTXO."""
        self.db.conn.execute("DELETE FROM lottery_wins WHERE height=0")
        self.db.conn.execute("DELETE FROM lottery_active WHERE height=0")
        self.db.conn.execute("DELETE FROM utxos WHERE height=0")
        self.db.conn.execute(
            """
            UPDATE blocks SET producer=NULL, subsidy=0, fees=0, winner_count=0, active_count=0
            WHERE height=0
            """
        )
        self.db.conn.execute(
            "UPDATE txs SET xfer_out=0 WHERE height=0 AND coinbase=1"
        )

    def _rewind(self, height: int) -> None:
        """Drop blocks above `height` (keep height). height=-1 wipes chain index."""
        self.db.conn.execute("DELETE FROM lottery_wins WHERE height>?", (height,))
        self.db.conn.execute("DELETE FROM lottery_active WHERE height>?", (height,))
        self.db.conn.execute("DELETE FROM asset_activity WHERE height>?", (height,))
        txids = [
            r["txid"]
            for r in self.db.conn.execute("SELECT txid FROM txs WHERE height>?", (height,)).fetchall()
        ]
        for txid in txids:
            # restore spent utxos that these txs consumed — rebuild from remaining
            self.db.conn.execute("DELETE FROM txio WHERE txid=?", (txid,))
            self.db.conn.execute("DELETE FROM utxos WHERE txid=?", (txid,))
            self.db.conn.execute("DELETE FROM txs WHERE txid=?", (txid,))
        self.db.conn.execute("DELETE FROM blocks WHERE height>?", (height,))
        # Rebuild utxo set from remaining txio is expensive; re-scan from 0 on deep reorgs.
        if height < 0:
            self.db.conn.execute("DELETE FROM utxos")
            self.db.conn.execute("DELETE FROM identities")
            self.db.conn.execute("DELETE FROM assets")
            self.db.conn.execute("DELETE FROM node_seen")
        else:
            self._rebuild_utxos()
        self.db.commit()

    def _rebuild_utxos(self) -> None:
        self.db.conn.execute("DELETE FROM utxos")
        self.db.conn.execute(
            """
            INSERT INTO utxos(txid, n, address, value, asset, asset_amount, height)
            SELECT o.txid, o.n, o.address, o.value, o.asset, o.asset_amount, t.height
            FROM txio o
            JOIN txs t ON t.txid = o.txid
            WHERE o.direction='out'
              AND NOT EXISTS (
                  SELECT 1 FROM txio i
                  WHERE i.direction='in' AND i.spent_txid=o.txid AND i.spent_n=o.n
              )
            """
        )

    def _refresh_lottery_live(self) -> None:
        nodes = self.rpc.try_call("getactivenodes", default=[]) or []
        now = int(time.time())
        live: dict[str, dict] = {}
        for n in nodes:
            nid = (n.get("id") or "").lower()
            script = n.get("script") or ""
            address = None
            if script:
                parsed = parse_vout_script(script, self.status.get("network") or "main")
                address = parsed.get("address")
            handle = (n.get("xaccount") or "").lower()
            live[nid] = {
                "id": nid,
                "script": script,
                "address": address,
                "xaccount": handle,
                "lastseen": n.get("lastseen"),
                "local": bool(n.get("local")),
            }
            self.db.conn.execute(
                """
                INSERT INTO node_seen(node_id, script, address, xaccount, last_height, last_seen)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET
                    script=excluded.script,
                    address=COALESCE(excluded.address, node_seen.address),
                    xaccount=COALESCE(NULLIF(excluded.xaccount,''), node_seen.xaccount),
                    last_seen=excluded.last_seen
                """,
                (nid, script, address, handle, self.status.get("indexed") or 0, n.get("lastseen") or now),
            )
            if handle and address:
                self.db.conn.execute(
                    """
                    INSERT INTO identities(handle, asset, address, node_id, first_height, txid)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(handle) DO UPDATE SET
                        address=COALESCE(identities.address, excluded.address),
                        node_id=COALESCE(identities.node_id, excluded.node_id)
                    """,
                    (handle, None, address, nid, self.status.get("indexed") or 0, None),
                )
        self.db.commit()
        with self._lock:
            self._live_nodes = live

    def _index_height(self, height: int, network: str) -> None:
        block_hash = self.rpc.call("getblockhash", height)
        block = self.rpc.call("getblock", block_hash, 2)
        txs = block.get("tx") or []
        interval = halving_interval_for_network(network)
        genesis_time = int(block.get("time") or 0) if height == 0 else None
        if genesis_time:
            self.db.set_meta("genesis_time", str(genesis_time))
        gtime = int(self.db.get_meta("genesis_time") or 0) or int(block.get("time") or 0)

        lottery = {
            "producer": None,
            "slot": slot_from_height(height, gtime) if height >= 0 else 0,
            "seed": None,
            "winners": [],
            "active": [],
            "xva": {},  # node_id (rpc hex) -> handle from this block's XVA1
            "subsidy": subsidy_at(height, interval),
            "fees": 0,
        }

        self.db.conn.execute(
            """
            INSERT OR REPLACE INTO blocks(height, hash, prev, time, nonce, size, tx_count,
                producer, subsidy, fees, lottery_slot, lottery_seed, winner_count, active_count)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                height,
                block.get("hash") or block_hash,
                block.get("previousblockhash"),
                int(block.get("time") or 0),
                int(block.get("nonce") or 0),
                int(block.get("size") or 0),
                len(txs),
                None,
                lottery["subsidy"],
                0,
                lottery["slot"],
                None,
                0,
                0,
            ),
        )

        for n, tx in enumerate(txs):
            if isinstance(tx, str):
                tx = self.rpc.call("getrawtransaction", tx, True)
            self._index_tx(tx, height, n, int(block.get("time") or 0), network, lottery)

        producer = lottery["winners"][0]["address"] if lottery["winners"] else None
        seed = None
        prev = block.get("previousblockhash")
        if prev and lottery["slot"]:
            try:
                seed = seed_for_draw(prev, lottery["slot"])
            except Exception:
                seed = None
        if lottery["active"] and seed:
            drawn = select_winners(lottery["active"], seed, max(1, len(lottery["winners"]) or 1))
            lottery["seed"] = seed
            # keep coinbase order as truth; seed is informational
            _ = drawn

        self.db.conn.execute(
            """
            UPDATE blocks SET producer=?, fees=?, lottery_seed=?, winner_count=?, active_count=?
            WHERE height=?
            """,
            (
                producer,
                lottery["fees"],
                lottery["seed"] or seed,
                len(lottery["winners"]),
                len(lottery["active"]),
                height,
            ),
        )

    def _index_tx(
        self,
        tx: dict,
        height: int,
        n: int,
        block_time: int,
        network: str,
        lottery: dict,
    ) -> None:
        txid = tx.get("txid") or tx.get("hash")
        vins = tx.get("vin") or []
        vouts = tx.get("vout") or []
        coinbase = any("coinbase" in vin for vin in vins)
        xfer_in = 0
        xfer_out = 0
        xid_handle = None
        identity = 0

        for vout in vouts:
            spk = vout.get("scriptPubKey") or {}
            hexscript = spk.get("hex") or ""
            parsed = parse_vout_script(hexscript, network) if hexscript else {}
            if parsed.get("xid1"):
                xid_handle = parsed["xid1"]
                identity = 1
            if parsed.get("xhb1") is not None:
                lottery["active"] = parsed["xhb1"]
                for nid in parsed["xhb1"]:
                    self.db.conn.execute(
                        "INSERT OR IGNORE INTO lottery_active(height, node_id) VALUES(?,?)",
                        (height, nid),
                    )
            if parsed.get("xva1"):
                for row in parsed["xva1"]:
                    nid = (row.get("node_id") or "").lower()
                    handle = (row.get("handle") or "").lower().lstrip("@")
                    if nid and handle:
                        lottery.setdefault("xva", {})[nid] = handle

        in_rows = []
        for vin in vins:
            if "coinbase" in vin:
                in_rows.append(
                    {
                        "address": None,
                        "value": 0,
                        "asset": None,
                        "asset_amount": 0,
                        "spent_txid": None,
                        "spent_n": None,
                    }
                )
                continue
            prev_txid = vin.get("txid")
            prev_n = vin.get("vout")
            prev = self.db.conn.execute(
                "SELECT address, value, asset, asset_amount FROM utxos WHERE txid=? AND n=?",
                (prev_txid, prev_n),
            ).fetchone()
            if prev:
                addr, val, asset, aamt = prev["address"], prev["value"], prev["asset"], prev["asset_amount"]
                xfer_in += int(val or 0)
                self.db.conn.execute("DELETE FROM utxos WHERE txid=? AND n=?", (prev_txid, prev_n))
            else:
                addr, val, asset, aamt = None, 0, None, 0
            in_rows.append(
                {
                    "address": addr,
                    "value": val,
                    "asset": asset,
                    "asset_amount": aamt,
                    "spent_txid": prev_txid,
                    "spent_n": prev_n,
                }
            )

        winner_rank = 0
        for vout in vouts:
            nout = int(vout.get("n") or 0)
            value = _atoms(vout.get("value"))
            spk = vout.get("scriptPubKey") or {}
            hexscript = spk.get("hex") or ""
            parsed = parse_vout_script(hexscript, network) if hexscript else {}
            addresses = spk.get("addresses") or []
            address = addresses[0] if addresses else parsed.get("address")
            script_type = spk.get("type") or parsed.get("script_type")
            asset_info = None
            rpc_asset = spk.get("asset")
            if isinstance(rpc_asset, dict) and rpc_asset.get("name"):
                asset_info = {
                    "name": rpc_asset.get("name"),
                    "amount": _atoms(rpc_asset.get("amount") or 0),
                    "kind": {
                        "new_asset": "new",
                        "reissue_asset": "reissue",
                        "transfer_asset": "transfer",
                    }.get(script_type or "", "transfer"),
                    "type_name": classify_asset_name(rpc_asset.get("name") or ""),
                    "units": None,
                    "reissuable": None,
                    "ipfs": rpc_asset.get("message") or "",
                }
            elif parsed.get("asset"):
                asset_info = parsed["asset"]

            genesis_cb = coinbase and height < 1
            if (
                genesis_cb
                or parsed.get("script_type") == "nulldata"
                or script_type in ("nulldata", "nonstandard")
            ):
                value_for_utxo = 0
            else:
                value_for_utxo = value
                xfer_out += value

            asset_name = asset_info["name"] if asset_info else None
            asset_amount = int(asset_info["amount"]) if asset_info else 0
            asset_kind = asset_info["kind"] if asset_info else None

            self.db.conn.execute(
                """
                INSERT OR REPLACE INTO txio(txid, n, direction, address, value, asset, asset_amount,
                    asset_kind, spent_txid, spent_n, coinbase, script_type)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    txid,
                    nout,
                    "out",
                    address,
                    value_for_utxo,
                    asset_name,
                    asset_amount,
                    asset_kind,
                    None,
                    None,
                    1 if coinbase else 0,
                    script_type,
                ),
            )

            if value_for_utxo or asset_name:
                self.db.conn.execute(
                    """
                    INSERT OR REPLACE INTO utxos(txid, n, address, value, asset, asset_amount, height)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (txid, nout, address, value_for_utxo, asset_name, asset_amount, height),
                )

            if asset_info:
                self._note_asset(asset_info, height, txid, address, xid_handle)

            if (
                coinbase
                and height >= 1
                and script_type not in ("nulldata",)
                and not parsed.get("xhb1")
                and not parsed.get("xva1")
            ):
                node_id = None
                if hexscript:
                    node_id = lottery_node_id_from_script_hex(hexscript)
                # Name this height from THIS coinbase's XVA1 only.
                # Live getactivenodes / node_seen is this minute and must not
                # label old blocks (GUI often only sees one peer).
                handle = None
                if node_id:
                    handle = (lottery.get("xva") or {}).get(node_id.lower())
                lottery["winners"].append(
                    {
                        "rank": winner_rank,
                        "node_id": node_id,
                        "address": address,
                        "amount": value,
                        "xaccount": handle,
                        "is_producer": 1 if winner_rank == 0 else 0,
                    }
                )
                self.db.conn.execute(
                    """
                    INSERT OR REPLACE INTO lottery_wins(height, rank, node_id, address, amount, xaccount, is_producer)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        height,
                        winner_rank,
                        node_id,
                        address,
                        value,
                        handle,
                        1 if winner_rank == 0 else 0,
                    ),
                )
                winner_rank += 1

        for i, row in enumerate(in_rows):
            self.db.conn.execute(
                """
                INSERT OR REPLACE INTO txio(txid, n, direction, address, value, asset, asset_amount,
                    asset_kind, spent_txid, spent_n, coinbase, script_type)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    txid,
                    i,
                    "in",
                    row["address"],
                    row["value"],
                    row["asset"],
                    row["asset_amount"],
                    None,
                    row["spent_txid"],
                    row["spent_n"],
                    1 if coinbase else 0,
                    "coinbase" if coinbase else "script",
                ),
            )

        fee = (xfer_in - xfer_out) if (not coinbase and xfer_in >= xfer_out) else 0
        if coinbase:
            lottery["fees"] = max(0, xfer_out - int(lottery["subsidy"] or 0))

        self.db.conn.execute(
            """
            INSERT OR REPLACE INTO txs(txid, height, n, time, coinbase, identity, xid_handle,
                xfer_in, xfer_out, fee, vin_count, vout_count)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                txid,
                height,
                n,
                block_time,
                1 if coinbase else 0,
                identity,
                xid_handle,
                xfer_in,
                xfer_out,
                fee,
                len(vins),
                len(vouts),
            ),
        )

        if xid_handle:
            dest = None
            for vout in vouts:
                spk = vout.get("scriptPubKey") or {}
                addrs = spk.get("addresses") or []
                parsed = parse_vout_script(spk.get("hex") or "", network)
                if parsed.get("asset") and parsed["asset"].get("kind") in ("new", "owner"):
                    dest = (addrs[0] if addrs else parsed.get("address")) or dest
                elif addrs and not parsed.get("xid1"):
                    dest = dest or addrs[0]
            asset_name = None
            for vout in vouts:
                spk = vout.get("scriptPubKey") or {}
                parsed = parse_vout_script(spk.get("hex") or "", network)
                asset = parsed.get("asset") or (
                    {"name": (spk.get("asset") or {}).get("name")} if isinstance(spk.get("asset"), dict) else None
                )
                if asset and asset.get("name") and not str(asset["name"]).endswith("!"):
                    asset_name = asset["name"]
                    break
            self.db.conn.execute(
                """
                INSERT INTO identities(handle, asset, address, node_id, first_height, txid)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(handle) DO UPDATE SET
                    asset=COALESCE(excluded.asset, identities.asset),
                    address=COALESCE(excluded.address, identities.address),
                    txid=COALESCE(identities.txid, excluded.txid),
                    first_height=COALESCE(identities.first_height, excluded.first_height)
                """,
                (xid_handle, asset_name, dest, None, height, txid),
            )
            if asset_name:
                self.db.conn.execute(
                    "UPDATE assets SET x_handle=? WHERE name=?", (xid_handle, asset_name)
                )

    def _note_asset(self, asset: dict, height: int, txid: str, address: str | None, xid_handle: str | None) -> None:
        name = asset.get("name")
        if not name:
            return
        kind = asset.get("type_name") or classify_asset_name(name)
        self.db.conn.execute(
            """
            INSERT INTO assets(name, kind, amount, units, reissuable, ipfs, created_height, created_txid, issuer, x_handle)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                amount=CASE WHEN excluded.kind IN ('root','sub','unique','owner') AND assets.created_height IS NOT NULL
                            THEN assets.amount ELSE COALESCE(excluded.amount, assets.amount) END,
                ipfs=COALESCE(NULLIF(excluded.ipfs,''), assets.ipfs),
                issuer=COALESCE(assets.issuer, excluded.issuer),
                x_handle=COALESCE(assets.x_handle, excluded.x_handle)
            """,
            (
                name,
                kind,
                int(asset.get("amount") or 0),
                asset.get("units") or 0,
                1 if asset.get("reissuable") else 0,
                asset.get("ipfs") or "",
                height if asset.get("kind") in ("new", "owner") else None,
                txid if asset.get("kind") in ("new", "owner") else None,
                address,
                xid_handle,
            ),
        )
        if asset.get("kind") in ("new", "reissue") and asset.get("amount"):
            # issuance / reissue increases circulating
            if asset.get("kind") == "reissue":
                self.db.conn.execute(
                    "UPDATE assets SET amount = COALESCE(amount,0) + ? WHERE name=?",
                    (int(asset.get("amount") or 0), name),
                )
        self.db.conn.execute(
            "INSERT INTO asset_activity(height, txid, name, kind, amount, address) VALUES(?,?,?,?,?,?)",
            (height, txid, name, asset.get("kind"), int(asset.get("amount") or 0), address),
        )
