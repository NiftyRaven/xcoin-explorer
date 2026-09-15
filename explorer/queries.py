"""Read-side SQL helpers."""

from __future__ import annotations

from typing import Any

from explorer.chain import (
    BURN_ADDRESSES_MAIN,
    GENESIS_TIME_MAIN,
    circulating_supply,
    halving_interval_for_network,
    last_paying_height,
    lifetime_supply,
    minutes_per_year,
    subsidy_at,
    winner_count,
)
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


def norm_handle(value: str | None) -> str:
    return (value or "").strip().lower().lstrip("@")


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
        share = self._share_for_txid(tx["txid"])
        if share:
            tx["host_share"] = share
        return tx

    def _share_for_txid(self, txid: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM lottery_shares WHERE txid=?", (txid,)
        ).fetchone()
        if not row:
            return None
        share = row_to_dict(row)
        share["guests"] = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                "SELECT * FROM lottery_share_guests WHERE txid=? ORDER BY n",
                (txid,),
            ).fetchall()
        ]
        return share

    def recent_shares(self, limit: int = 20) -> list[dict]:
        limit = paginate(limit, 20)
        rows = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                """
                SELECT * FROM lottery_shares
                ORDER BY height DESC, txid
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        for share in rows:
            share["guests"] = [
                row_to_dict(r)
                for r in self.db.conn.execute(
                    "SELECT * FROM lottery_share_guests WHERE txid=? ORDER BY n",
                    (share["txid"],),
                ).fetchall()
            ]
        return rows

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
            "guest_shares": [
                row_to_dict(r)
                for r in self.db.conn.execute(
                    """
                    SELECT s.txid, s.height, s.host_handle, s.guest_percent, g.amount
                    FROM lottery_share_guests g
                    JOIN lottery_shares s ON s.txid = g.txid
                    WHERE g.address=?
                    ORDER BY s.height DESC LIMIT 20
                    """,
                    (addr,),
                ).fetchall()
            ],
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

    def lottery_members(
        self,
        q: str = "",
        scope: str = "eligible",
        hat_handles: list[str] | None = None,
        heartbeat_handles: list[str] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict:
        """Directory of lottery handles: eligible now, or every handle seen in XVA1."""
        limit = paginate(limit, 200, max_n=1000)
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0
        needle = norm_handle(q)
        scope = "all" if str(scope or "").lower() == "all" else "eligible"

        if hat_handles is None:
            hat = {norm_handle(r.get("xaccount")) for r in self.last_xva()}
        else:
            hat = {norm_handle(h) for h in hat_handles}
        hat.discard("")
        beat = {norm_handle(h) for h in (heartbeat_handles or [])}
        beat.discard("")

        known: dict[str, dict[str, Any]] = {}

        def row_for(handle: str) -> dict[str, Any]:
            return known.setdefault(
                handle,
                {
                    "handle": handle,
                    "address": None,
                    "node_id": None,
                    "asset": None,
                    "wins": 0,
                    "earned": 0,
                    "hat_blocks": 0,
                    "first_hat": None,
                    "last_hat": None,
                    "last_seen": None,
                },
            )

        for r in self.db.conn.execute(
            "SELECT handle, asset, address, node_id FROM identities"
        ).fetchall():
            h = norm_handle(r["handle"])
            if not h:
                continue
            row = row_for(h)
            row["asset"] = r["asset"] or row["asset"]
            row["address"] = r["address"] or row["address"]
            row["node_id"] = r["node_id"] or row["node_id"]

        for r in self.db.conn.execute(
            "SELECT node_id, address, xaccount, last_seen FROM node_seen"
        ).fetchall():
            h = norm_handle(r["xaccount"])
            if not h:
                continue
            row = row_for(h)
            row["address"] = row["address"] or r["address"]
            row["node_id"] = row["node_id"] or r["node_id"]
            row["last_seen"] = r["last_seen"]

        for r in self.db.conn.execute(
            """
            SELECT
                lower(xaccount) AS handle,
                COUNT(*) AS hat_blocks,
                MIN(height) AS first_hat,
                MAX(height) AS last_hat
            FROM lottery_active
            WHERE xaccount IS NOT NULL AND TRIM(xaccount) != ''
            GROUP BY lower(xaccount)
            """
        ).fetchall():
            h = norm_handle(r["handle"])
            if not h:
                continue
            row = row_for(h)
            row["hat_blocks"] = int(r["hat_blocks"] or 0)
            row["first_hat"] = r["first_hat"]
            row["last_hat"] = r["last_hat"]

        for r in self.db.conn.execute(
            """
            SELECT
                lower(xaccount) AS handle,
                COUNT(*) AS wins,
                SUM(amount) AS earned
            FROM lottery_wins
            WHERE height>=1 AND xaccount IS NOT NULL AND TRIM(xaccount) != ''
            GROUP BY lower(xaccount)
            """
        ).fetchall():
            h = norm_handle(r["handle"])
            if not h:
                continue
            row = row_for(h)
            row["wins"] = int(r["wins"] or 0)
            row["earned"] = int(r["earned"] or 0)

        for h in hat | beat:
            row_for(h)

        items: list[dict[str, Any]] = []
        eligible_total = 0
        for handle, row in known.items():
            in_hat = handle in hat
            heartbeat = handle in beat
            eligible = in_hat or heartbeat
            if eligible:
                eligible_total += 1
            if needle and needle not in handle:
                continue
            if scope != "all" and not eligible:
                continue
            items.append(
                {
                    **row,
                    "in_hat": in_hat,
                    "heartbeat": heartbeat,
                    "eligible": eligible,
                }
            )
        items.sort(key=lambda x: (not x["eligible"], -(x.get("wins") or 0), x["handle"]))
        return {
            "query": q,
            "scope": scope,
            "total": len(items),
            "eligible_total": eligible_total,
            "known_total": len(known),
            "items": items[offset : offset + limit],
        }

    def handle_profile(
        self,
        handle: str,
        hat_handles: list[str] | None = None,
        heartbeat_handles: list[str] | None = None,
    ) -> dict | None:
        name = norm_handle(handle)
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in name):
            return None
        directory = self.lottery_members(
            q=name,
            scope="all",
            hat_handles=hat_handles,
            heartbeat_handles=heartbeat_handles,
            limit=50,
        )
        member = next((m for m in directory["items"] if m["handle"] == name), None)
        if member is None:
            member = {
                "handle": name,
                "address": None,
                "node_id": None,
                "asset": None,
                "wins": 0,
                "earned": 0,
                "hat_blocks": 0,
                "first_hat": None,
                "last_hat": None,
                "last_seen": None,
                "in_hat": name in {norm_handle(h) for h in (hat_handles or [])},
                "heartbeat": name in {norm_handle(h) for h in (heartbeat_handles or [])},
                "eligible": False,
            }
            member["eligible"] = bool(member["in_hat"] or member["heartbeat"])
        ident = self.db.conn.execute(
            "SELECT * FROM identities WHERE handle=?", (name,)
        ).fetchone()
        wins = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                """
                SELECT * FROM lottery_wins
                WHERE height>=1 AND lower(xaccount)=?
                ORDER BY height DESC LIMIT 50
                """,
                (name,),
            ).fetchall()
        ]
        appearances = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                """
                SELECT height, node_id, n
                FROM lottery_active
                WHERE lower(xaccount)=?
                ORDER BY height DESC LIMIT 40
                """,
                (name,),
            ).fetchall()
        ]
        shares_sent = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                """
                SELECT * FROM lottery_shares
                WHERE lower(host_handle)=?
                ORDER BY height DESC LIMIT 20
                """,
                (name,),
            ).fetchall()
        ]
        shares_received = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                """
                SELECT s.txid, s.height, s.host_handle, s.guest_percent, g.amount, g.address
                FROM lottery_share_guests g
                JOIN lottery_shares s ON s.txid = g.txid
                WHERE lower(g.handle)=?
                ORDER BY s.height DESC LIMIT 20
                """,
                (name,),
            ).fetchall()
        ]
        return {
            **member,
            "found": bool(
                member.get("hat_blocks")
                or member.get("wins")
                or member.get("address")
                or member.get("eligible")
                or ident
                or appearances
                or shares_sent
                or shares_received
            ),
            "identity": row_to_dict(ident) if ident else None,
            "wins_recent": wins,
            "appearances": appearances,
            "shares_sent": shares_sent,
            "shares_received": shares_received,
        }

    def chain_stats(self, pulse: int = 180) -> dict:
        """Observatory aggregates: emission, hat fairness, pulse, identity."""
        try:
            pulse = max(24, min(int(pulse or 180), 720))
        except (TypeError, ValueError):
            pulse = 180
        net = self.network()
        interval = halving_interval_for_network(net)
        height = max(int(self.db.indexed_height() or 0), 0)
        genesis_time = int(self.db.get_meta("genesis_time") or GENESIS_TIME_MAIN)
        last_pay = last_paying_height(interval)
        issued = circulating_supply(height, interval)
        lifetime = lifetime_supply(interval)
        era = height // interval if interval else 0
        era_end = (era + 1) * interval - 1
        next_halving = era_end + 1 if subsidy_at(height, interval) else None
        mpy = minutes_per_year()

        hat_sizes = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                """
                SELECT height, COUNT(*) AS n
                FROM lottery_active
                WHERE height>=1 AND xaccount IS NOT NULL AND TRIM(xaccount)!=''
                GROUP BY height
                ORDER BY height DESC
                LIMIT ?
                """,
                (pulse,),
            ).fetchall()
        ]
        hat_sizes.reverse()

        handles = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                """
                SELECT
                    lower(a.xaccount) AS handle,
                    COUNT(*) AS hat_blocks,
                    MIN(a.height) AS first_hat,
                    MAX(a.height) AS last_hat,
                    SUM(1.0 / h.n) AS expected_wins
                FROM lottery_active a
                JOIN (
                    SELECT height, COUNT(*) AS n
                    FROM lottery_active
                    WHERE height>=1 AND xaccount IS NOT NULL AND TRIM(xaccount)!=''
                    GROUP BY height
                ) h ON h.height = a.height
                WHERE a.height>=1 AND a.xaccount IS NOT NULL AND TRIM(a.xaccount)!=''
                GROUP BY lower(a.xaccount)
                """
            ).fetchall()
        ]
        wins = {
            (r["handle"] or "").lower(): (int(r["wins"] or 0), int(r["earned"] or 0))
            for r in self.db.conn.execute(
                """
                SELECT lower(xaccount) AS handle, COUNT(*) AS wins, SUM(amount) AS earned
                FROM lottery_wins
                WHERE height>=1 AND xaccount IS NOT NULL AND TRIM(xaccount)!=''
                GROUP BY lower(xaccount)
                """
            ).fetchall()
        }
        for row in handles:
            w, earned = wins.get(row["handle"], (0, 0))
            row["wins"] = w
            row["earned"] = earned
            exp = float(row.get("expected_wins") or 0)
            row["expected_wins"] = round(exp, 4)
            row["luck"] = round(w / exp, 4) if exp > 0 else None
        handles.sort(key=lambda x: (-(x.get("wins") or 0), x["handle"]))

        kinds = [
            row_to_dict(r)
            for r in self.db.conn.execute(
                """
                SELECT kind, COUNT(*) AS n
                FROM assets
                WHERE kind IS NULL OR kind != 'owner'
                GROUP BY kind
                ORDER BY n DESC
                """
            ).fetchall()
        ]
        ipfs_n = self.db.conn.execute(
            "SELECT COUNT(*) AS c FROM assets WHERE ipfs IS NOT NULL AND TRIM(ipfs)!=''"
        ).fetchone()["c"]
        unique_winners = self.db.conn.execute(
            """
            SELECT COUNT(DISTINCT lower(xaccount)) AS c FROM lottery_wins
            WHERE height>=1 AND xaccount IS NOT NULL AND TRIM(xaccount)!=''
            """
        ).fetchone()["c"]
        paydays = self.db.conn.execute(
            """
            SELECT COUNT(*) AS c FROM lottery_wins
            WHERE height>=1 AND xaccount IS NOT NULL AND TRIM(xaccount)!=''
            """
        ).fetchone()["c"]
        top_share = 0.0
        if paydays and handles:
            top_share = (handles[0].get("wins") or 0) / paydays
        hhi = 0.0
        if paydays:
            hhi = sum(((h.get("wins") or 0) / paydays) ** 2 for h in handles)

        eras = []
        for i in range(8):
            start = 1 if i == 0 else i * interval
            end = (i + 1) * interval - 1
            sub = subsidy_at(start, interval)
            if sub <= 0:
                break
            eras.append(
                {
                    "era": i,
                    "start": start,
                    "end": end,
                    "subsidy_atoms": sub,
                    "winners": winner_count(start, interval),
                    "current": start <= height <= end,
                }
            )

        return {
            "height": height,
            "genesis_time": genesis_time,
            "minutes_lived": max(height, 0),
            "minutes_this_era": (
                max(0, height - (1 if era == 0 else era * interval) + 1) if height >= 1 else 0
            ),
            "era": era,
            "era_end": era_end,
            "next_halving_height": next_halving,
            "minutes_to_halving": max(0, (next_halving or 0) - height) if next_halving else 0,
            "years_to_halving": round(max(0, (next_halving or 0) - height) / mpy, 2) if next_halving else 0,
            "last_paying_height": last_pay,
            "years_of_emission": round(last_pay / mpy, 1),
            "emission_progress": (height / last_pay) if last_pay else 0,
            "issued_atoms": issued,
            "lifetime_atoms": lifetime,
            "subsidy_atoms": subsidy_at(max(height, 1), interval),
            "winner_count_now": winner_count(max(height, 1), interval),
            "paydays": int(paydays or 0),
            "unique_winners": int(unique_winners or 0),
            "top_handle_share": round(top_share, 4),
            "hhi": round(hhi, 4),
            "handles": handles,
            "hat_pulse": hat_sizes,
            "eras": eras,
            "assets": {"by_kind": kinds, "ipfs": int(ipfs_n or 0)},
        }

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
            handle = norm_handle(raw)
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
            if handle:
                seen_ids = {r["id"] for r in results if r.get("type") == "identity"}
                for item in self.lottery_members(q=handle, scope="all", limit=8).get("items") or []:
                    hid = item.get("handle")
                    if not hid or hid in seen_ids:
                        continue
                    if handle != hid and handle not in hid:
                        continue
                    results.append(
                        {
                            "type": "identity",
                            "id": hid,
                            "label": f"@{hid}",
                            "eligible": bool(item.get("eligible")),
                            "wins": item.get("wins") or 0,
                        }
                    )
                    seen_ids.add(hid)
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
