"""Read-side SQL helpers."""

from __future__ import annotations

from typing import Any

from explorer.chain import BURN_ADDRESSES_MAIN, circulating_supply, halving_interval_for_network, subsidy_at, winner_count
from explorer.db import Database
from explorer.decode import classify_search
from explorer.ipfs import attach_ipfs_fields


def row_to_dict(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def paginate(limit: int, default: int = 25, max_n: int = 100) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, max_n))


class Queries:
    def __init__(self, db: Database):
        self.db = db

    def network(self) -> str:
        return self.db.get_meta("network") or "main"

    def status(self, indexer_status: dict, rpc_ok: bool, rpc_error: str, extra: dict | None = None) -> dict:
        net = self.network()
        height = self.db.indexed_height()
        interval = halving_interval_for_network(net)
        supply = circulating_supply(max(height, 0), interval)
        stats = self.db.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM blocks) AS blocks,
                (SELECT COUNT(*) FROM txs) AS txs,
                (SELECT COUNT(*) FROM assets WHERE kind != 'owner') AS assets,
                (SELECT COUNT(*) FROM identities) AS identities,
                (SELECT COUNT(*) FROM lottery_wins) AS wins
            """
        ).fetchone()
        out = {
            "network": net,
            "network_label": {
                "main": "mainnet",
                "test": "testnet",
                "regtest": "practice (regtest)",
            }.get(net, net),
            "indexed_height": height,
            "tip": indexer_status.get("tip", height),
            "indexing": bool(indexer_status.get("indexing")),
            "rpc_connected": rpc_ok,
            "rpc_error": rpc_error,
            "supply_atoms": supply,
            "subsidy_atoms": subsidy_at(max(height, 0) + 1, interval),
            "winner_count_next": winner_count(max(height, 0) + 1, interval),
            "counts": row_to_dict(stats),
            "best_hash": self.db.get_meta("best_hash"),
            "genesis_time": int(self.db.get_meta("genesis_time") or 0),
        }
        if extra:
            out.update(extra)
        return out

    def recent_blocks(self, limit: int = 20, before: int | None = None) -> list[dict]:
        limit = paginate(limit)
        sql = """
            SELECT b.*, w.xaccount AS winner_handle
            FROM blocks b
            LEFT JOIN lottery_wins w ON w.height = b.height AND w.rank = 0
        """
        if before is None:
            rows = self.db.conn.execute(
                sql + " ORDER BY b.height DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                sql + " WHERE b.height < ? ORDER BY b.height DESC LIMIT ?",
                (before, limit),
            ).fetchall()
        return [row_to_dict(r) for r in rows]

    def block(self, key: str) -> dict | None:
        if key.isdigit():
            row = self.db.conn.execute("SELECT * FROM blocks WHERE height=?", (int(key),)).fetchone()
        else:
            row = self.db.conn.execute("SELECT * FROM blocks WHERE hash=?", (key.lower(),)).fetchone()
            if not row:
                row = self.db.conn.execute("SELECT * FROM blocks WHERE hash=?", (key,)).fetchone()
        if not row:
            return None
        block = row_to_dict(row)
        txs = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                "SELECT * FROM txs WHERE height=? ORDER BY n ASC", (block["height"],)
            ).fetchall()
        ]
        wins = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                "SELECT * FROM lottery_wins WHERE height=? ORDER BY rank ASC", (block["height"],)
            ).fetchall()
        ]
        active = [
            r["node_id"]
            for r in self.db.conn.execute(
                "SELECT node_id FROM lottery_active WHERE height=?", (block["height"],)
            ).fetchall()
        ]
        block["txs"] = txs
        block["lottery"] = {
            "winners": wins,
            "active_ids": active,
            "handles": [
                r["xaccount"]
                for r in (
                    self.db.conn.execute(
                        "SELECT xaccount FROM lottery_active WHERE height=? AND xaccount IS NOT NULL AND TRIM(xaccount)!='' ORDER BY n ASC, node_id",
                        (block["height"],),
                    ).fetchall()
                )
            ],
        }
        block["winner_handle"] = (wins[0].get("xaccount") if wins else None)
        return block

    def tx(self, txid: str) -> dict | None:
        row = self.db.conn.execute("SELECT * FROM txs WHERE txid=?", (txid,)).fetchone()
        if not row:
            row = self.db.conn.execute("SELECT * FROM txs WHERE txid=?", (txid.lower(),)).fetchone()
        if not row:
            return None
        tx = row_to_dict(row)
        ins = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                "SELECT * FROM txio WHERE txid=? AND direction='in' ORDER BY n", (tx["txid"],)
            ).fetchall()
        ]
        outs = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                "SELECT * FROM txio WHERE txid=? AND direction='out' ORDER BY n", (tx["txid"],)
            ).fetchall()
        ]
        tx["vin"] = ins
        tx["vout"] = outs
        tx["lottery_handles"] = []
        tx["winner_handle"] = None
        if tx.get("height") is not None:
            b = self.db.conn.execute(
                "SELECT hash, time FROM blocks WHERE height=?", (tx["height"],)
            ).fetchone()
            if b:
                tx["block_hash"] = b["hash"]
                tx["block_time"] = b["time"]
            if tx.get("coinbase"):
                wins = [
                    row_to_dict(r)
                    for r in self.db.conn.execute(
                        "SELECT * FROM lottery_wins WHERE height=? ORDER BY rank ASC",
                        (tx["height"],),
                    ).fetchall()
                ]
                active = [
                    row_to_dict(r)
                    for r in self.db.conn.execute(
                        "SELECT node_id, xaccount FROM lottery_active WHERE height=? ORDER BY n ASC, node_id",
                        (tx["height"],),
                    ).fetchall()
                ]
                handles: list[str] = []
                seen: set[str] = set()
                for row in active:
                    h = (row.get("xaccount") or "").lower().lstrip("@")
                    if h and h not in seen:
                        seen.add(h)
                        handles.append(h)
                if not handles:
                    for row in wins:
                        h = (row.get("xaccount") or "").lower().lstrip("@")
                        if h and h not in seen:
                            seen.add(h)
                            handles.append(h)
                tx["lottery_handles"] = handles
                if wins:
                    tx["winner_handle"] = (wins[0].get("xaccount") or None)
                    if tx["winner_handle"]:
                        tx["winner_handle"] = str(tx["winner_handle"]).lower().lstrip("@")
        return tx

    def address(self, addr: str, limit: int = 50) -> dict:
        limit = paginate(limit, 50)
        xfer = self.db.conn.execute(
            "SELECT COALESCE(SUM(value),0) AS v FROM utxos WHERE address=? AND (asset IS NULL OR asset='')",
            (addr,),
        ).fetchone()["v"]
        assets = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                """
                SELECT asset AS name, COALESCE(SUM(asset_amount),0) AS amount
                FROM utxos WHERE address=? AND asset IS NOT NULL AND asset!=''
                GROUP BY asset ORDER BY amount DESC
                """,
                (addr,),
            ).fetchall()
        ]
        txs = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                """
                SELECT DISTINCT t.*
                FROM txs t
                JOIN txio io ON io.txid = t.txid
                WHERE io.address=?
                ORDER BY t.height DESC, t.n DESC
                LIMIT ?
                """,
                (addr, limit),
            ).fetchall()
        ]
        wins = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                "SELECT * FROM lottery_wins WHERE address=? ORDER BY height DESC LIMIT 50",
                (addr,),
            ).fetchall()
        ]
        ident = self.db.conn.execute(
            "SELECT * FROM identities WHERE address=?", (addr,)
        ).fetchone()
        received = self.db.conn.execute(
            "SELECT COALESCE(SUM(value),0) AS v FROM txio WHERE address=? AND direction='out'",
            (addr,),
        ).fetchone()["v"]
        sent = self.db.conn.execute(
            "SELECT COALESCE(SUM(value),0) AS v FROM txio WHERE address=? AND direction='in'",
            (addr,),
        ).fetchone()["v"]
        return {
            "address": addr,
            "balance_atoms": int(xfer or 0),
            "received_atoms": int(received or 0),
            "sent_atoms": int(sent or 0),
            "burn": BURN_ADDRESSES_MAIN.get(addr),
            "assets": assets,
            "txs": txs,
            "lottery_wins": wins,
            "identity": row_to_dict(ident) if ident else None,
        }

    def assets(self, q: str = "", kind: str = "", limit: int = 50, offset: int = 0) -> dict:
        limit = paginate(limit, 50)
        offset = max(0, int(offset or 0))
        args: list[Any] = []
        clauses = ["(kind IS NULL OR kind != 'owner')"]
        if q:
            clauses.append("(name LIKE ? OR IFNULL(x_handle,'') LIKE ?)")
            args.extend([f"%{q.upper()}%", f"%{q.lstrip('@').lower()}%"])
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        where_sql = " AND ".join(clauses)
        total = self.db.conn.execute(
            f"SELECT COUNT(*) AS c FROM assets WHERE {where_sql}", args
        ).fetchone()["c"]
        rows = self.db.conn.execute(
            f"SELECT * FROM assets WHERE {where_sql} ORDER BY created_height DESC, name LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        return {"total": total, "items": [attach_ipfs_fields(row_to_dict(r)) for r in rows]}

    def asset(self, name: str, limit: int = 50) -> dict | None:
        row = self.db.conn.execute("SELECT * FROM assets WHERE name=?", (name,)).fetchone()
        if not row:
            row = self.db.conn.execute("SELECT * FROM assets WHERE name=?", (name.upper(),)).fetchone()
        if not row:
            return None
        asset = row_to_dict(row)
        holders = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                """
                SELECT address, COALESCE(SUM(asset_amount),0) AS amount
                FROM utxos WHERE asset=?
                GROUP BY address HAVING amount > 0
                ORDER BY amount DESC LIMIT 100
                """,
                (asset["name"],),
            ).fetchall()
        ]
        activity = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                "SELECT * FROM asset_activity WHERE name=? ORDER BY id DESC LIMIT ?",
                (asset["name"], paginate(limit, 50)),
            ).fetchall()
        ]
        ident = self.db.conn.execute(
            "SELECT * FROM identities WHERE asset=?", (asset["name"],)
        ).fetchone()
        asset["holders"] = holders
        asset["activity"] = activity
        asset["identity"] = row_to_dict(ident) if ident else None
        asset["holder_count"] = self.db.conn.execute(
            "SELECT COUNT(DISTINCT address) AS c FROM utxos WHERE asset=? AND asset_amount>0",
            (asset["name"],),
        ).fetchone()["c"]
        return attach_ipfs_fields(asset)

    def store_ipfs(self, name: str, cid: str) -> None:
        if not name or not cid:
            return
        self.db.conn.execute(
            "UPDATE assets SET ipfs=COALESCE(NULLIF(ipfs,''), ?) WHERE name=?",
            (cid, name),
        )
        self.db.commit()

    def lottery_history(self, limit: int = 40, before: int | None = None) -> list[dict]:
        limit = paginate(limit, 40)
        if before is None:
            heights = [
                r["height"]
                for r in self.db.conn.execute(
                    "SELECT DISTINCT height FROM lottery_wins WHERE height>=1 ORDER BY height DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
        else:
            heights = [
                r["height"]
                for r in self.db.conn.execute(
                    "SELECT DISTINCT height FROM lottery_wins WHERE height>=1 AND height < ? ORDER BY height DESC LIMIT ?",
                    (before, limit),
                ).fetchall()
            ]
        out = []
        for h in heights:
            block = self.db.conn.execute(
                "SELECT hash, time, lottery_slot, lottery_seed, producer, subsidy, fees, active_count FROM blocks WHERE height=?",
                (h,),
            ).fetchone()
            wins = [
                row_to_dict(r)
                for r in self.db.conn.execute(
                    "SELECT * FROM lottery_wins WHERE height=? ORDER BY rank", (h,)
                ).fetchall()
            ]
            item = {"height": h, "winners": wins}
            if block:
                item.update(row_to_dict(block))
            out.append(item)
        return out

    def lottery_leaders(self, limit: int = 50) -> list[dict]:
        limit = paginate(limit, 50)
        rows = self.db.conn.execute(
            """
            SELECT
                lower(xaccount) AS who,
                lower(xaccount) AS xaccount,
                COUNT(*) AS wins,
                SUM(amount) AS earned
            FROM lottery_wins
            WHERE height>=1 AND xaccount IS NOT NULL AND TRIM(xaccount) != ''
            GROUP BY lower(xaccount)
            ORDER BY wins DESC, earned DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]

    def last_xva(self) -> list[dict]:
        """Handles (+ ids) from the last indexed block that carried XVA1."""
        row = self.db.conn.execute(
            """
            SELECT MAX(height) AS h FROM lottery_active
            WHERE xaccount IS NOT NULL AND TRIM(xaccount) != ''
            """
        ).fetchone()
        height = row["h"] if row else None
        if height is None:
            row = self.db.conn.execute(
                """
                SELECT MAX(height) AS h FROM lottery_wins
                WHERE height>=1 AND xaccount IS NOT NULL AND TRIM(xaccount) != ''
                """
            ).fetchone()
            height = row["h"] if row else None
        if height is None:
            return []
        active = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                "SELECT node_id, xaccount FROM lottery_active WHERE height=? ORDER BY n ASC, node_id",
                (height,),
            ).fetchall()
        ]
        if any((r.get("xaccount") or "").strip() for r in active):
            return active
        return [
            row_to_dict(r)
            for r in self.db.conn.execute(
                "SELECT node_id, xaccount FROM lottery_wins WHERE height=? ORDER BY rank",
                (height,),
            ).fetchall()
        ]

    def rich_list(self, limit: int = 50) -> list[dict]:
        limit = paginate(limit, 50)
        rows = self.db.conn.execute(
            """
            SELECT address, COALESCE(SUM(value),0) AS balance
            FROM utxos
            WHERE address IS NOT NULL AND address != '' AND (asset IS NULL OR asset='')
            GROUP BY address
            HAVING balance > 0
            ORDER BY balance DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]

    def search(self, q: str) -> dict:
        raw = (q or "").strip()
        kind = classify_search(raw)
        results: list[dict] = []
        if kind == "empty":
            return {"query": raw, "kind": kind, "results": []}
        if kind == "height":
            b = self.block(raw)
            if b:
                results.append({"type": "block", "id": str(b["height"]), "label": f"Block {b['height']}"})
        if kind in ("hash", "query"):
            ql = raw.lower()
            b = self.db.conn.execute("SELECT height, hash FROM blocks WHERE hash=?", (ql,)).fetchone()
            if b:
                results.append({"type": "block", "id": b["hash"], "label": f"Block {b['height']}"})
            t = self.db.conn.execute("SELECT txid, height FROM txs WHERE txid=?", (ql,)).fetchone()
            if t:
                results.append({"type": "tx", "id": t["txid"], "label": f"Transaction at height {t['height']}"})
        if kind in ("address", "query"):
            a = self.db.conn.execute(
                "SELECT address FROM txio WHERE address=? LIMIT 1", (raw,)
            ).fetchone()
            if a:
                results.append({"type": "address", "id": raw, "label": raw})
        if kind in ("handle", "query", "asset"):
            handle = raw.lstrip("@").lower()
            ident = self.db.conn.execute(
                "SELECT * FROM identities WHERE handle=?", (handle,)
            ).fetchone()
            if ident:
                results.append(
                    {
                        "type": "identity",
                        "id": ident["handle"],
                        "label": f"@{ident['handle']}",
                        "asset": ident["asset"],
                        "address": ident["address"],
                    }
                )
            name = raw.upper().lstrip("@")
            assets = self.db.conn.execute(
                "SELECT name, kind FROM assets WHERE name=? OR name LIKE ? LIMIT 8",
                (name, f"%{name}%"),
            ).fetchall()
            for a in assets:
                results.append({"type": "asset", "id": a["name"], "label": a["name"], "kind": a["kind"]})
        if kind == "node_id":
            wins = self.db.conn.execute(
                "SELECT address, xaccount FROM lottery_wins WHERE node_id=? LIMIT 1",
                (raw.lower(),),
            ).fetchone()
            if wins:
                results.append(
                    {
                        "type": "node",
                        "id": raw.lower(),
                        "label": wins["xaccount"] and f"@{wins['xaccount']}" or wins["address"] or raw,
                        "address": wins["address"],
                    }
                )
        if not results and kind == "query":
            # fuzzy asset / handle
            like = f"%{raw}%"
            for a in self.db.conn.execute(
                "SELECT name, kind FROM assets WHERE name LIKE ? LIMIT 8", (like.upper(),)
            ).fetchall():
                results.append({"type": "asset", "id": a["name"], "label": a["name"], "kind": a["kind"]})
        return {"query": raw, "kind": kind, "results": results}
