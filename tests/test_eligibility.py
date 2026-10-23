"""Eligibility checklist: hat, 180s heartbeat, last block, blue check, second wallet."""

import re
import time
from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.db import Database
from explorer.eligibility import FIX_VPN, HEARTBEAT_LIMIT_SECONDS, assemble
from explorer.indexer import Indexer
from explorer.queries import Queries

LIGHOST = "debb6500d17c4ae40b3c84539a34c13af0d8eae6"
SHARED = "45e83cdb10f54e3affda5569d01c5723419af92b"
LI_NODE = "e04d68a7255185c90185108536a77e0250f2269c"
IDENTITY = "Xj4BxrnpKmjs9KTQk2cL8qgS2iW4cdfcE2"
PAYOUT = "XsSQccyppwrUXH9aYFShjLrQwyUnyJmBAR"
SHARED_ADDR = "XgeKTcJYQophXAgePYB43gFuHYDVegTkiz"

# 2026-10-23 13:05:20 ET, the end of the real check-in window.
LAST_BEAT = 1_792_775_120
LAST_HAT_TIME = LAST_BEAT - 3 * 3600
NOW = LAST_BEAT + 2 * 3600
TIP = 59652
LAST_HAT = 59423


class _Wallet:
    connected = True

    def __init__(self, info=None, nodes=None):
        self.info = info or {}
        self.nodes = nodes or []

    def try_call(self, method, *params, default=None):
        if method == "getlotteryinfo":
            return self.info
        if method == "getactivenodes":
            return self.nodes
        return default


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "elig.db")


def _block(db: Database, height: int, when: int) -> None:
    db.conn.execute(
        "INSERT INTO blocks(height, hash, time, tx_count, active_count, winner_count) VALUES(?,?,?,1,1,1)",
        (height, f"{height:064x}", when),
    )


def _active(db: Database, height: int, node_id: str, handle: str) -> None:
    db.conn.execute(
        "INSERT INTO lottery_active(height, node_id, xaccount, n) VALUES(?,?,?,0)",
        (height, node_id, handle),
    )


def _win(db: Database, height: int, node_id: str, handle: str, address: str) -> None:
    db.conn.execute(
        """
        INSERT INTO lottery_wins(height, rank, node_id, address, amount, xaccount, is_producer)
        VALUES(?, 0, ?, ?, 500000000000, ?, 1)
        """,
        (height, node_id, address, handle),
    )


def _client(tmp_path: Path, info=None, nodes=None):
    db = _db(tmp_path)
    rpc = _Wallet(info, nodes)
    app = create_app(Queries(db), Indexer(db, rpc), rpc)
    return db, TestClient(app)


def _seed_dave(db: Database) -> None:
    _block(db, LAST_HAT, LAST_HAT_TIME)
    _block(db, TIP, NOW - 30)
    db.conn.execute(
        "INSERT INTO identities(handle, asset, address, node_id, first_height) VALUES(?,?,?,?,?)",
        ("davefromlighost", "DAVEFROMLIGHOST", IDENTITY, LIGHOST, 45456),
    )
    db.conn.execute(
        "INSERT INTO identities(handle, asset, address, node_id, first_height) VALUES(?,?,?,?,?)",
        ("davefromli", "DAVEFROMLI", "XkykYfvYVrc1py1sYJaHxAcW7tBEqUdziC", "db4b9d8ba1771b2b5b6534eba5a474aaf4af7bca", 33488),
    )
    _active(db, LAST_HAT, LIGHOST, "davefromlighost")
    _active(db, 59430, SHARED, "davefromli")
    _win(db, LAST_HAT - 9, LIGHOST, "davefromlighost", PAYOUT)
    db.conn.execute(
        "INSERT INTO node_seen(node_id, address, xaccount, last_height, last_seen) VALUES(?,?,?,?,?)",
        (LIGHOST, PAYOUT, "davefromlighost", LAST_HAT, LAST_HAT_TIME),
    )
    db.conn.execute(
        "INSERT INTO node_seen(node_id, address, xaccount, last_height, last_seen) VALUES(?,?,?,?,?)",
        (SHARED, SHARED_ADDR, "davefromlighost", TIP, LAST_BEAT),
    )
    db.commit()


def _check(body: dict, check_id: str) -> dict:
    return next(row for row in body["checks"] if row["id"] == check_id)


def test_heartbeats_after_the_hat_are_likely_and_name_the_second_wallet(tmp_path: Path):
    db, client = _client(
        tmp_path,
        info={"stamped_handles": ["davefromli"], "height": TIP, "attestation_required": True},
        nodes=[{"id": LI_NODE, "xaccount": "davefromli", "lastseen": NOW - 20, "attested": True}],
    )
    _seed_dave(db)
    body = client.get("/api/eligibility?q=@davefromlighost").json()
    assert body["handle"] == "davefromlighost"
    assert body["identity_address"] == IDENTITY
    assert body["payout_address"] == PAYOUT
    assert body["rejection_reasons_exposed"] is False
    assert body["in_hat"] is False
    hat = _check(body, "in_hat")
    assert hat["ok"] is False and hat["likely"] is True
    assert hat["fix"] == FIX_VPN
    assert "38443" in hat["fix"]
    assert "Likely" in hat["detail"]
    beat = _check(body, "heartbeat")
    assert beat["ok"] is False
    assert beat["age_seconds"] > HEARTBEAT_LIMIT_SECONDS
    assert "180" in beat["detail"]
    last = _check(body, "last_hat")
    assert last["ok"] is False
    assert f"Out of hat since block {LAST_HAT}" in last["detail"]
    assert str(TIP) in last["detail"]
    verified = _check(body, "x_verified")
    assert verified["ok"] is None
    assert "not available" in verified["detail"]
    other = _check(body, "other_node")
    assert other["ok"] is False and other["likely"] is True
    assert "@davefromli" in other["detail"]
    assert "in the hat" in other["detail"]
    assert other["fix"]
    node = _check(body, "registered_node")
    assert node["ok"] is True
    assert LIGHOST in node["detail"]
    assert body["checks"]
    db.close()


def test_address_search_uses_the_payout_and_keeps_the_identity(tmp_path: Path):
    db, client = _client(tmp_path, info={"stamped_handles": []}, nodes=[])
    _seed_dave(db)
    body = client.get("/api/eligibility", params={"q": PAYOUT}).json()
    assert body["handle"] == "davefromlighost"
    assert body["via"] == "address"
    assert body["payout_address"] == PAYOUT
    assert body["identity_address"] == IDENTITY
    listed = client.get("/api/lottery/members?scope=all&q=davefromlighost").json()
    row = listed["items"][0]
    assert row["identity_address"] == IDENTITY
    assert row["payout_address"] == PAYOUT
    assert row["address"] == IDENTITY
    by_node = client.get("/api/eligibility", params={"q": LIGHOST}).json()
    assert by_node["handle"] == "davefromlighost"
    assert by_node["via"] == "node_id"
    db.close()


def test_stamped_checkin_that_missed_the_draw_is_not_called_a_vpn(tmp_path: Path):
    now = int(time.time())
    db, client = _client(
        tmp_path,
        info={"stamped_handles": ["alice"], "attestation_required": True, "local_xaccount": "alice", "local_x_verified": True},
        nodes=[{"id": "aa", "xaccount": "zekedrops", "lastseen": now - 10, "attested": True}],
    )
    _block(db, 10, now - 40)
    _active(db, 10, "aa", "zekedrops")
    db.conn.execute("INSERT INTO identities(handle, node_id) VALUES('zekedrops','aa')")
    db.commit()
    body = client.get("/api/eligibility?q=zekedrops").json()
    hat = _check(body, "in_hat")
    assert hat["likely"] is True
    assert hat["fix"] != FIX_VPN
    assert "next draw" in hat["fix"]
    db.close()


def test_unstamped_checkin_uses_the_stamp_fact_not_an_inference(tmp_path: Path):
    now = int(time.time())
    db, client = _client(
        tmp_path,
        info={"stamped_handles": ["alice"], "attestation_required": True},
        nodes=[{"id": "bb", "xaccount": "zekedrops", "lastseen": now - 5, "attested": False}],
    )
    _block(db, 4, now - 600)
    _active(db, 4, "bb", "zekedrops")
    db.conn.execute("INSERT INTO identities(handle, node_id, address) VALUES('zekedrops','bb','Xj4BxrnpKmjs9KTQk2cL8qgS2iW4cdfcE2')")
    db.commit()
    body = client.get("/api/eligibility?q=@zekedrops").json()
    hat = _check(body, "in_hat")
    assert hat["likely"] is False
    assert "stamped" in hat["detail"].lower()
    assert hat["fix"] != FIX_VPN
    verified = _check(body, "x_verified")
    assert verified["ok"] is False
    assert verified["likely"] is False
    db.close()


def test_two_live_nodes_for_one_handle_are_observed(tmp_path: Path):
    now = int(time.time())
    db, client = _client(
        tmp_path,
        info={"stamped_handles": [], "local_xaccount": "zekedrops", "local_x_verified": False, "local_attested": False},
        nodes=[
            {"id": "aa", "xaccount": "zekedrops", "lastseen": now - 5, "attested": False},
            {"id": "bb", "xaccount": "zekedrops", "lastseen": now - 5, "attested": False},
        ],
    )
    db.conn.execute("INSERT INTO identities(handle, node_id) VALUES('zekedrops','aa')")
    db.commit()
    body = client.get("/api/eligibility?q=zekedrops").json()
    other = _check(body, "other_node")
    assert other["ok"] is False and other["likely"] is False
    assert "More than one node" in other["detail"]
    verified = _check(body, "x_verified")
    assert verified["ok"] is False
    assert "own session" in verified["detail"]
    db.close()


def test_in_hat_passes_the_checklist(tmp_path: Path):
    now = 10_000
    db, client = _client(
        tmp_path,
        info={"stamped_handles": ["alice"], "height": 3, "local_xaccount": "alice", "local_x_verified": True, "local_attested": True},
        nodes=[{"id": "aa", "xaccount": "alice", "lastseen": now - 10, "attested": True}],
    )
    _block(db, 3, now - 20)
    _active(db, 3, "aa", "alice")
    db.conn.execute(
        "INSERT INTO identities(handle, address, node_id) VALUES(?,?,?)",
        ("alice", IDENTITY, "aa"),
    )
    db.conn.execute(
        "INSERT INTO node_seen(node_id, address, xaccount, last_height, last_seen) VALUES(?,?,?,?,?)",
        ("aa", PAYOUT, "alice", 3, now - 10),
    )
    _win(db, 3, "aa", "alice", PAYOUT)
    db.commit()
    # The index heartbeat is fresh relative to the live node, but `now` in the
    # request is wall clock. Pin the clock through assemble for the pass case.
    index = Queries(db).eligibility_index("alice")
    body = assemble(
        index,
        hat_handles={"alice"},
        hat_source="stamped_handles",
        live_nodes=[{"id": "aa", "handle": "alice", "lastseen": now - 10, "attested": True}],
        live={"local_xaccount": "alice", "local_x_verified": True, "local_attested": True, "stamped_handles": ["alice"], "height": 3},
        now=now,
        tip_height=3,
        tip_time=now - 20,
    )
    assert body["in_hat"] is True
    assert all(row["ok"] is True for row in body["checks"])
    assert _check(body, "heartbeat")["age_seconds"] == 10
    assert "Out of hat since" not in _check(body, "last_hat")["detail"]
    db.close()


def test_empty_and_nonsense_queries(tmp_path: Path):
    _db_obj, client = _client(tmp_path)
    assert client.get("/api/eligibility").status_code == 400
    assert client.get("/api/eligibility?q=not%20a%20name").status_code == 400
    missing = client.get("/api/eligibility?q=@nobody").json()
    assert missing["handle"] == "nobody"
    assert missing["found"] is False
    assert _check(missing, "in_hat")["ok"] is False
    _db_obj.close()


def test_copy_avoids_promises_and_keeps_the_help_link():
    root = Path(__file__).resolve().parent.parent
    py = (root / "explorer" / "eligibility.py").read_text(encoding="utf-8")
    js = (root / "web" / "js" / "app.js").read_text(encoding="utf-8")
    css = (root / "web" / "css" / "app.css").read_text(encoding="utf-8")
    html = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\b(earn|yield|apy|profit|guaranteed)\b", py, re.I)
    panel = js[js.index("function eligibilityHtml"): js.index("function bindEligibilitySearch")]
    setup = js[js.index("function setupHtml"): js.index("async function pageSetup")]
    for chunk in (panel, setup):
        assert not re.search(r"\b(earn|yield|apy|profit|guaranteed)\b", chunk, re.I)
    assert "Check my eligibility" in js
    assert "VPN / firewall setup" in js
    assert "38443" in js
    assert "Split-tunnel the wallet" in js
    assert "one wallet per handle" in js.lower() or "Keep one wallet per handle." in js
    assert "Keep the PC awake." in js
    assert "synced to the tip" in js
    assert "normally shows 0" in js
    assert "Where wins are paid." in js
    assert "infoTip(" in panel
    assert 'class="info-btn"' in css or ".info-btn" in css
    assert "prefers-color-scheme: light" in css
    assert "--bg: #07080d" in css
    assert "explorer-public-1" in html
    assert "pages-1-eligibility" in html
