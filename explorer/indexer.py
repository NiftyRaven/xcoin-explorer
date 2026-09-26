"""Pull blocks from xcoind and fill the local SQLite index."""

from __future__ import annotations

import threading
import time
from typing import Any

from explorer.amounts import xfer_to_atoms
from explorer.chain import (
    BLOCK_TIME_SECONDS,
    block_poll_delay,
    classify_asset_name,
    halving_interval_for_network,
    slot_from_height,
    subsidy_at,
)
from explorer.db import Database
from explorer.decode import (
    coinbase_lottery,
    normalize_ipfs,
    parse_vout_script,
    seed_for_draw,
    select_winners,
)
from explorer.hostshare import detect_host_share
from explorer.rpc import RpcError, XCoinRPC

# Bump to force a full XVA1 rebuild of lottery_wins / lottery_active.
LOTTERY_INDEX_V = "4"
# Bump to re-scan indexed spends for 1.0.14 host/guest share-outs.
SHARE_INDEX_V = "1"


def _atoms(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return xfer_to_atoms(value)


class Indexer:
    def __init__(
        self,
        db: Database,
        rpc: XCoinRPC,
        batch_size: int = 40,
        poll_seconds: float = BLOCK_TIME_SECONDS,
    ):
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
            self._stop.wait(self._next_poll_delay())
        self.status["running"] = False

    def _tip_block_time(self) -> int | None:
        row = self.db.conn.execute(
            "SELECT time FROM blocks ORDER BY height DESC LIMIT 1"
        ).fetchone()
        if not row or row["time"] is None:
            return None
        try:
            t = int(row["time"])
        except (TypeError, ValueError):
            return None
        return t if t > 0 else None

    def _next_poll_delay(self) -> float:
        return block_poll_delay(
            indexing=bool(self.status.get("indexing")),
            now=time.time(),
            poll_seconds=self.poll_seconds,
            tip_time=self._tip_block_time(),
        )

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
        self._rebuild_shares()
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
        if self.db.get_meta("lottery_v") == LOTTERY_INDEX_V:
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
        self.db.set_meta("lottery_v", LOTTERY_INDEX_V)
        self.db.commit()

    def _rebuild_shares(self) -> None:
        """Tag already-indexed spends that match 1.0.14 guest-split math."""
        if self.db.get_meta("share_v") == SHARE_INDEX_V:
            return
        self.db.conn.execute("DELETE FROM lottery_share_guests")
        self.db.conn.execute("DELETE FROM lottery_shares")
        txids = [
            r["txid"]
            for r in self.db.conn.execute(
                "SELECT txid FROM txs WHERE coinbase=0 AND height>=1"
            ).fetchall()
        ]
        for txid in txids:
            self._maybe_index_share(txid)
        self.db.set_meta("share_v", SHARE_INDEX_V)
        self.db.commit()

    def _handle_for_address(self, addr: str | None) -> str | None:
        if not addr:
            return None
        row = self.db.conn.execute(
            "SELECT handle FROM identities WHERE address=?", (addr,)
        ).fetchone()
        if row and row["handle"]:
            return str(row["handle"]).lower().lstrip("@")
        row = self.db.conn.execute(
            """
            SELECT xaccount FROM lottery_wins
            WHERE address=? AND xaccount IS NOT NULL AND TRIM(xaccount)!=''
            LIMIT 1
            """,
            (addr,),
        ).fetchone()
        if row and row["xaccount"]:
            return str(row["xaccount"]).lower().lstrip("@")
        return None

    def _maybe_index_share(self, txid: str) -> None:
        self.db.conn.execute("DELETE FROM lottery_share_guests WHERE txid=?", (txid,))
        self.db.conn.execute("DELETE FROM lottery_shares WHERE txid=?", (txid,))
        row = self.db.conn.execute(
            "SELECT coinbase, height FROM txs WHERE txid=?", (txid,)
        ).fetchone()
        if not row or row["coinbase"]:
            return
        vins = self.db.conn.execute(
            """
            SELECT spent_txid, spent_n, address, value
            FROM txio WHERE txid=? AND direction='in'
            """,
            (txid,),
        ).fetchall()
        win = None
        for vin in vins:
            spent = vin["spent_txid"]
            if not spent:
                continue
            cb = self.db.conn.execute(
                "SELECT height, coinbase FROM txs WHERE txid=?", (spent,)
            ).fetchone()
            if not cb or not cb["coinbase"] or int(cb["height"] or 0) < 1:
                continue
            spent_amt = int(vin["value"] or 0)
            if spent_amt <= 0:
                continue
            w = self.db.conn.execute(
                """
                SELECT height, xaccount, address, amount FROM lottery_wins
                WHERE height=? AND (address=? OR amount=?)
                ORDER BY CASE WHEN address=? THEN 0 ELSE 1 END, rank
                """,
                (cb["height"], vin["address"], spent_amt, vin["address"]),
            ).fetchone()
            if not w:
                w = self.db.conn.execute(
                    """
                    SELECT height, xaccount, address, amount FROM lottery_wins
                    WHERE height=? ORDER BY rank
                    """,
                    (cb["height"],),
                ).fetchone()
            if not w:
                continue
            win = {
                "host_txid": spent,
                "host_height": int(cb["height"]),
                "host_handle": (str(w["xaccount"]).lower().lstrip("@") if w["xaccount"] else None),
                "host_address": vin["address"] or w["address"],
                "win_amount": spent_amt,
            }
            break
        if not win:
            return
        outs = [
            (r["address"], int(r["value"] or 0))
            for r in self.db.conn.execute(
                "SELECT address, value FROM txio WHERE txid=? AND direction='out' ORDER BY n",
                (txid,),
            ).fetchall()
        ]
        found = detect_host_share(win["win_amount"], win["host_address"], outs)
        if not found:
            return
        self.db.conn.execute(
            """
            INSERT OR REPLACE INTO lottery_shares(
                txid, height, host_txid, host_height, host_handle, host_address,
                win_amount, pot_amount, guest_percent, guest_count, guest_each)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                txid,
                row["height"],
                win["host_txid"],
                win["host_height"],
                win["host_handle"],
                win["host_address"],
                win["win_amount"],
                found["pot"],
                found["percent"],
                len(found["guests"]),
                found["guest_each"],
            ),
        )
        for i, guest in enumerate(found["guests"]):
            addr = guest.get("address")
            self.db.conn.execute(
                """
                INSERT OR REPLACE INTO lottery_share_guests(txid, n, address, handle, amount)
                VALUES(?,?,?,?,?)
                """,
                (
                    txid,
                    i,
                    addr,
                    self._handle_for_address(addr),
                    guest.get("amount") or 0,
                ),
            )

    def _upsert_lottery_active(
        self, height: int, node_id: str | None, handle: str | None, n: int = 0
    ) -> None:
        nid = (node_id or "").lower()
        if not nid:
            return
        xaccount = (handle or "").lower().lstrip("@") or None
        self.db.conn.execute(
            """
            INSERT INTO lottery_active(height, node_id, xaccount, n) VALUES(?,?,?,?)
            ON CONFLICT(height, node_id) DO UPDATE SET
                xaccount=COALESCE(excluded.xaccount, lottery_active.xaccount),
                n=excluded.n
            """,
            (height, nid, xaccount, n),
        )

    def _write_lottery_from_coinbase(self, tx: dict, height: int, network: str) -> None:
        """Parse XVA1 + value>0 payees on an already-indexed coinbase (rebuild path)."""
        if height < 1:
            return
        lot = coinbase_lottery(tx.get("vout") or [], network)
        self.db.conn.execute("DELETE FROM lottery_wins WHERE height=?", (height,))
        self.db.conn.execute("DELETE FROM lottery_active WHERE height=?", (height,))
        active_ids = [r["node_id"] for r in lot["xva"]] or lot["xhb1"]
        for i, nid in enumerate(active_ids):
            self._upsert_lottery_active(
                height, nid, lot["id_to_handle"].get((nid or "").lower()), i
            )
        for w in lot["winners"]:
            self.db.conn.execute(
                """
                INSERT OR REPLACE INTO lottery_wins(height, rank, node_id, address, amount, xaccount, is_producer)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    height,
                    w["rank"],
                    w.get("node_id"),
                    w.get("address"),
                    w.get("amount") or 0,
                    w.get("xaccount"),
                    w.get("is_producer") or 0,
                ),
            )

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
        self.db.conn.execute(
            """
            DELETE FROM lottery_share_guests
            WHERE txid IN (SELECT txid FROM lottery_shares WHERE height>?)
            """,
            (height,),
        )
        self.db.conn.execute("DELETE FROM lottery_shares WHERE height>?", (height,))
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

        if coinbase and height >= 1:
            lot = coinbase_lottery(vouts, network)
            lottery["xva"] = lot["id_to_handle"]
            lottery["active"] = [r["node_id"] for r in lot["xva"]] or lot["xhb1"]
            for i, nid in enumerate(lottery["active"]):
                self._upsert_lottery_active(
                    height, nid, lot["id_to_handle"].get((nid or "").lower()), i
                )
            lottery["winners"] = lot["winners"]
            for w in lot["winners"]:
                self.db.conn.execute(
                    """
                    INSERT OR REPLACE INTO lottery_wins(height, rank, node_id, address, amount, xaccount, is_producer)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        height,
                        w["rank"],
                        w.get("node_id"),
                        w.get("address"),
                        w.get("amount") or 0,
                        w.get("xaccount"),
                        w.get("is_producer") or 0,
                    ),
                )

        for vout in vouts:
            spk = vout.get("scriptPubKey") or {}
            hexscript = spk.get("hex") or ""
            parsed = parse_vout_script(hexscript, network) if hexscript else {}
            # XID1 is the asset-root claim (e.g. NFTRVN). Never lottery identity.
            if parsed.get("xid1") and not coinbase:
                xid_handle = parsed["xid1"]
                identity = 1

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
            parsed_asset = parsed.get("asset") if isinstance(parsed.get("asset"), dict) else {}
            if isinstance(rpc_asset, dict) and rpc_asset.get("name"):
                units = rpc_asset.get("units")
                if units is None:
                    units = parsed_asset.get("units")
                asset_info = {
                    "name": rpc_asset.get("name"),
                    "amount": _atoms(rpc_asset.get("amount") or 0),
                    "kind": {
                        "new_asset": "new",
                        "reissue_asset": "reissue",
                        "transfer_asset": "transfer",
                    }.get(script_type or "", "transfer"),
                    "type_name": classify_asset_name(rpc_asset.get("name") or ""),
                    "units": units,
                    "reissuable": rpc_asset.get("reissuable", parsed_asset.get("reissuable")),
                    "ipfs": normalize_ipfs(rpc_asset.get("message") or "") or (rpc_asset.get("message") or ""),
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
            op_return = parsed.get("op_return") or None

            self.db.conn.execute(
                """
                INSERT OR REPLACE INTO txio(txid, n, direction, address, value, asset, asset_amount,
                    asset_kind, spent_txid, spent_n, coinbase, script_type, op_return)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    op_return,
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

        if not coinbase:
            self._maybe_index_share(txid)

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
                x_handle=COALESCE(assets.x_handle, excluded.x_handle),
                units=CASE WHEN excluded.units IS NOT NULL AND excluded.units > 0 THEN excluded.units ELSE assets.units END
            """,
            (
                name,
                kind,
                int(asset.get("amount") or 0),
                asset.get("units") or 0,
                1 if asset.get("reissuable") else 0,
                normalize_ipfs(asset.get("ipfs") or "") or (asset.get("ipfs") or ""),
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
