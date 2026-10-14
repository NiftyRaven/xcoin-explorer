"""Hat stats merge every node id for a handle and leave the old profile alone."""

from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.config import Settings
from explorer.db import Database
from explorer.hatstats import absence_reason, compute_hat_stats, indexed_lookup_plans, luck_note
from explorer.indexer import Indexer
from explorer.queries import Queries
from explorer.rpc import XCoinRPC


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "hat.db")


def _block(db: Database, height: int, when: int, active: int = 0) -> None:
    db.conn.execute(
        """
        INSERT INTO blocks(height, hash, time, tx_count, active_count, winner_count)
        VALUES(?, ?, ?, 1, ?, 1)
        """,
        (height, f"{height:064x}", when, active),
    )


def _active(db: Database, height: int, node_id: str, handle: str | None, n: int = 0) -> None:
    db.conn.execute(
        "INSERT INTO lottery_active(height, node_id, xaccount, n) VALUES(?,?,?,?)",
        (height, node_id, handle, n),
    )


def _win(db: Database, height: int, node_id: str, handle: str | None, address: str, amount: int = 500_000_000_000) -> None:
    db.conn.execute(
        """
        INSERT INTO lottery_wins(height, rank, node_id, address, amount, xaccount, is_producer)
        VALUES(?, 0, ?, ?, ?, ?, 1)
        """,
        (height, node_id, address, amount, handle),
    )


def test_luck_note_below_average_uses_a_friendly_odds():
    # Tail near 1/8, so the sentence can say "about 1 in 8".
    note = luck_note(3, 8.0, 15.3)
    assert note["tone"] == "below"
    assert note["one_in"] == 8
    assert "Below average luck" in note["text"]
    assert "About 1 in 8 players" in note["text"]
    assert "Each draw is random" in note["text"]


def test_luck_note_average_and_empty():
    average = luck_note(8, 8.0, 8.0)
    assert average["tone"] == "average"
    assert average["one_in"] is None
    assert "random" in average["text"].lower()
    empty = luck_note(0, 0.0, 0.0)
    assert empty["tone"] == "none"
    assert "random" in empty["text"].lower()


def test_absence_reasons():
    assert absence_reason(
        in_hat=True, heartbeat=True, attested=True, ever_in_hat=True,
        attestation_required=True, local_verified=True,
    ) is None
    assert absence_reason(
        in_hat=False, heartbeat=False, attested=None, ever_in_hat=True,
        attestation_required=True, local_verified=None,
    ) == "no_heartbeat"
    assert absence_reason(
        in_hat=False, heartbeat=False, attested=None, ever_in_hat=False,
        attestation_required=True, local_verified=None,
    ) == "not_verified"
    assert absence_reason(
        in_hat=False, heartbeat=True, attested=False, ever_in_hat=True,
        attestation_required=True, local_verified=True,
    ) == "attestation"
    assert absence_reason(
        in_hat=False, heartbeat=True, attested=True, ever_in_hat=True,
        attestation_required=True, local_verified=True,
    ) == "recently_restarted"
    assert absence_reason(
        in_hat=False, heartbeat=False, attested=None, ever_in_hat=True,
        attestation_required=True, local_verified=False,
    ) == "not_verified"


def test_merges_every_node_id_for_one_handle(tmp_path: Path):
    db = _db(tmp_path)
    now = 1_000_000
    # Three nodes for zeke. The identity row keeps only the oldest id.
    db.conn.execute(
        "INSERT INTO identities(handle, address, node_id, first_height) VALUES(?,?,?,?)",
        ("zekedrops", "Xoldaddr111111111111111111111111111", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 1),
    )
    db.conn.execute(
        "INSERT INTO node_seen(node_id, address, xaccount, last_height, last_seen) VALUES(?,?,?,?,?)",
        ("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "Xmidaddr1111111111111111111111111111", "zekedrops", 2, now - 100),
    )
    # h1: old node, handle filled in. Hat of 2.
    _block(db, 1, now - 90_000, 2)
    _active(db, 1, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "zekedrops", 0)
    _active(db, 1, "cccccccccccccccccccccccccccccccccccccccc", "alice", 1)
    _win(db, 1, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "zekedrops", "Xoldpay11111111111111111111111111111", 500_000_000_000)
    # h2: middle node, handle missing on the draw row. Still zeke's node.
    _block(db, 2, now - 1000, 2)
    _active(db, 2, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", None, 0)
    _active(db, 2, "cccccccccccccccccccccccccccccccccccccccc", "alice", 1)
    _win(db, 2, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", None, "Xmidpay11111111111111111111111111111", 500_000_072_024)
    # h3: newest node, both of zeke's nodes in one draw count once. Hat of 4.
    _block(db, 3, now - 60, 4)
    _active(db, 3, "dddddddddddddddddddddddddddddddddddddddd", "zekedrops", 0)
    _active(db, 3, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "zekedrops", 1)
    _active(db, 3, "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "bob", 2)
    _active(db, 3, "ffffffffffffffffffffffffffffffffffffffff", "cara", 3)
    # A different handle's node must not be pulled in.
    _block(db, 4, now - 30, 1)
    _active(db, 4, "9999999999999999999999999999999999999999", "alice", 0)
    _win(db, 4, "9999999999999999999999999999999999999999", "alice", "Xalice111111111111111111111111111111", 500_000_000_000)
    db.commit()

    stats = compute_hat_stats(db, "zekedrops", now=now, recent=10)
    assert stats is not None
    assert stats["draws_all"] == 3  # heights 1, 2, 3 — not alice's height 4
    assert stats["draws_since_joined"] == 4
    assert stats["included_percent"] == 75.0
    assert stats["draws_24h"] == 2  # heights 2 and 3; height 1 is older than a day
    assert stats["draws_24h_total"] == 3  # heights 2, 3, 4
    # 1/2 + 1/2 + 1/4. The two nodes in height 3 are one draw.
    assert stats["expected_wins"] == 1.25
    # The blank-handle win on the middle node is still zeke's.
    assert stats["wins"] == 2
    assert stats["earned"] == 500_000_000_000 + 500_000_072_024
    ids = [n["node_id"] for n in stats["nodes"]]
    assert ids == [
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "dddddddddddddddddddddddddddddddddddddddd",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ] or set(ids) == {
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "dddddddddddddddddddddddddddddddddddddddd",
    }
    assert "9999999999999999999999999999999999999999" not in ids
    paid = {row["address"] for row in stats["payout_addresses"]}
    assert "Xoldpay11111111111111111111111111111" in paid
    assert "Xmidpay11111111111111111111111111111" in paid
    assert "Xalice111111111111111111111111111111" not in paid
    recent = {row["height"]: row for row in stats["recent_draws"]}
    assert recent[3]["in_hat"] is True
    assert recent[3]["hat_size"] == 4
    assert recent[3]["winner"] is None
    assert recent[1]["won"] is True
    assert recent[1]["winner"] == "zekedrops"
    assert recent[4]["in_hat"] is False
    assert recent[4]["winner"] == "alice"
    assert stats["last_heartbeat"] == now - 100
    assert "random" in stats["luck"]["text"].lower()
    db.close()


def test_invalid_handle_is_rejected(tmp_path: Path):
    db = _db(tmp_path)
    assert compute_hat_stats(db, "") is None
    assert compute_hat_stats(db, "bad handle") is None
    assert compute_hat_stats(db, "ok_name")["draws_all"] == 0
    db.close()


class _Wallet:
    connected = True

    def __init__(self, info, nodes):
        self.info = info
        self.nodes = nodes

    def try_call(self, method, *params, default=None):
        if method == "getlotteryinfo":
            return self.info
        if method == "getactivenodes":
            return self.nodes
        return default


def _client(tmp_path: Path, info=None, nodes=None):
    db = Database(tmp_path / "api.db")
    rpc = _Wallet(info or {}, nodes or [])
    indexer = Indexer(db, rpc)
    app = create_app(Queries(db), indexer, rpc)
    return db, TestClient(app)


def test_hat_stats_api_is_additive(tmp_path: Path):
    db, client = _client(
        tmp_path,
        info={
            "stamped_handles": ["alice"],
            "attestation_required": True,
            "local_xaccount": "other",
            "local_x_verified": True,
        },
        nodes=[
            {"id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "xaccount": "zekedrops", "lastseen": 50, "attested": False},
        ],
    )
    _block(db, 1, 1_000, 1)
    _active(db, 1, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "zekedrops")
    db.conn.execute(
        "INSERT INTO identities(handle, node_id) VALUES(?,?)",
        ("zekedrops", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
    )
    db.commit()

    old = client.get("/api/handle/zekedrops")
    assert old.status_code == 200
    body = old.json()
    for extra in ("expected_wins", "recent_draws", "reason_text", "draws_24h", "included_percent", "payout_addresses", "luck"):
        assert extra not in body
    assert body["node_id"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert body["in_hat"] is False
    assert "wins_recent" in body and "appearances" in body

    stats = client.get("/api/handle/zekedrops/hat-stats")
    assert stats.status_code == 200
    hat = stats.json()
    assert hat["draws_all"] == 1
    assert hat["in_hat"] is False
    # Heartbeat is present, but the node is not attested.
    assert hat["reason"] == "attestation"
    assert "Attestation" in hat["reason_text"]
    assert hat["last_heartbeat"] == 50
    assert {n["node_id"] for n in hat["nodes"]} == {
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    }
    again = client.get("/api/handle/zekedrops/hat-stats")
    assert again.json()["expected_wins"] == hat["expected_wins"]
    assert client.get("/api/handle/nope nope").status_code == 400
    db.close()


def test_live_reason_recently_restarted_and_in_hat(tmp_path: Path):
    db, client = _client(
        tmp_path,
        info={"stamped_handles": ["alice"], "attestation_required": True, "local_x_verified": True, "local_xaccount": "alice"},
        nodes=[
            {"id": "aa", "xaccount": "zekedrops", "lastseen": 9, "attested": True},
            {"id": "bb", "xaccount": "alice", "lastseen": 9, "attested": True},
        ],
    )
    _block(db, 1, 10, 1)
    _active(db, 1, "aa", "zekedrops")
    db.conn.execute(
        "INSERT INTO identities(handle, node_id) VALUES('alice','bb')"
    )
    db.commit()
    zeke = client.get("/api/handle/zekedrops/hat-stats").json()
    assert zeke["in_hat"] is False
    assert zeke["reason"] == "recently_restarted"
    assert "Recently restarted" in zeke["reason_text"]
    alice = client.get("/api/handle/alice/hat-stats").json()
    assert alice["in_hat"] is True
    assert alice["reason"] is None
    offline = client.get("/api/handle/zekedrops").json()
    assert "reason" not in offline
    db.close()


def test_page_keeps_old_identity_markup_and_adds_tips(tmp_path: Path):
    root = Path(__file__).resolve().parent.parent
    js = (root / "web" / "js" / "app.js").read_text(encoding="utf-8")
    css = (root / "web" / "css" / "app.css").read_text(encoding="utf-8")
    html = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert "In this minute’s hat" in js
    assert "Hat blocks" in js
    assert "Guest receipts" in js
    assert "hatStatsHtml" in js
    assert "/hat-stats" in js
    assert "info-btn" in js
    assert "producer" not in js
    assert "8000" not in js
    assert ".info-btn" in css
    assert "prefers-color-scheme" not in css
    assert "?v=explorer-public-1" in html
    db = Database(tmp_path / "offline.db")
    rpc = XCoinRPC(Settings())
    indexer = Indexer(db, rpc)
    client = TestClient(create_app(Queries(db), indexer, rpc))
    assert client.get("/api/handle/someone/hat-stats").status_code == 404
    assert client.get("/api/handle/someone").status_code == 200
    db.close()


def test_lookup_plans_use_expression_indexes(tmp_path: Path):
    db = _db(tmp_path)
    plans = indexed_lookup_plans(db.conn)
    assert "idx_active_xaccount_norm" in plans["active_handle"]
    assert "SCAN lottery_active" not in plans["active_handle"]
    assert "idx_wins_xaccount_norm" in plans["wins_handle"]
    assert "SCAN lottery_wins" not in plans["wins_handle"]
    assert "idx_active_node_lower" in plans["active_node"]
    assert "SCAN lottery_active" not in plans["active_node"]
    db.close()


def test_unknown_handle_is_404_and_cache_is_per_handle(tmp_path: Path):
    db, client = _client(
        tmp_path,
        info={"stamped_handles": ["zekedrops"], "attestation_required": True},
        nodes=[{"id": "aa", "xaccount": "zekedrops", "lastseen": 40, "attested": True}],
    )
    _block(db, 1, 1_000, 1)
    _active(db, 1, "aa", "zekedrops")
    db.commit()
    missing = client.get("/api/handle/nobody")
    assert missing.status_code == 200
    hat_missing = client.get("/api/handle/nobody/hat-stats")
    assert hat_missing.status_code == 404
    assert hat_missing.json()["error"] == "not found"
    assert client.get("/api/handle/nope nope/hat-stats").status_code == 400

    first = client.get("/api/handle/zekedrops/hat-stats").json()
    assert first["draws_all"] == 1
    assert first["in_hat"] is True
    _block(db, 2, 2_000, 1)
    _active(db, 2, "aa", "zekedrops")
    db.commit()
    client.app.state.rpc.nodes = [
        {"id": "aa", "xaccount": "zekedrops", "lastseen": 99, "attested": False}
    ]
    second = client.get("/api/handle/zekedrops/hat-stats").json()
    # A new block and a new heartbeat stay out of the answer until the time limit.
    assert second["draws_all"] == first["draws_all"]
    assert second["last_heartbeat"] == first["last_heartbeat"]
    assert second["reason"] == first["reason"]
    assert client.app.state.hat_cache._ttl == 60.0
    assert client.app.state.hat_cache._max_keys == 256
    assert client.app.state.live_node_cache._ttl == 60.0
    assert client.app.state.live_node_cache._max_keys == 8
    db.close()
