"""Address history and /api/stats stay cheap under load."""

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.config import Settings
from explorer.db import Database
from explorer.indexer import Indexer
from explorer.queries import Queries
from explorer.rpc import XCoinRPC
from explorer.statcache import StaleWhileRefreshCache

A = "XmLv1ZYu8qMsGTsWvD9N7C7AFcK844nHwF"
B = "XdJLXZpNpY7bYKvkzhHqzfRUYLXNQTQ2JR"

OLD_TX_SQL = """
    SELECT t.*,
      (SELECT COALESCE(SUM(value), 0) FROM txio
        WHERE txid = t.txid AND address = ? AND direction = 'out') AS addr_received,
      (SELECT COALESCE(SUM(value), 0) FROM txio
        WHERE txid = t.txid AND address = ? AND direction = 'in') AS addr_sent
    FROM txs t
    WHERE EXISTS (SELECT 1 FROM txio io WHERE io.txid = t.txid AND io.address = ?)
    ORDER BY t.height IS NULL DESC, t.height DESC, t.n DESC
    LIMIT ?
"""


def _seed(db: Database) -> None:
    conn = db.conn
    rows_tx, rows_io = [], []
    for i in range(400):
        txid = f"{i:064x}"
        height = None if i >= 395 else i // 3  # a few mempool rows, several txs per block
        rows_tx.append((txid, height, i % 3, 1_700_000_000 + i, 1 if i % 7 == 0 else 0, i * 10))
        n = 0
        if i % 2 == 0:
            rows_io.append((txid, n, "out", A, 1000 + i, None, 0)); n += 1
        if i % 3 == 0:
            rows_io.append((txid, n, "in", A, 500 + i, None, 0)); n += 1
        if i % 5 == 0:
            # Two outputs to the same address in one tx, one carrying an owner token.
            rows_io.append((txid, n, "out", A, 7, "LAUNCH_XFER!", 100_000_000)); n += 1
        if i % 4 == 0:
            rows_io.append((txid, n, "out", B, 300 + i, None, 0)); n += 1
        rows_io.append((txid, n, "out", f"Xother{i}", 1, None, 0))
    conn.executemany(
        "INSERT INTO txs(txid,height,n,time,coinbase,xfer_out) VALUES(?,?,?,?,?,?)", rows_tx
    )
    conn.executemany(
        "INSERT INTO txio(txid,n,direction,address,value,asset,asset_amount) VALUES(?,?,?,?,?,?,?)",
        rows_io,
    )
    # A txio row whose tx is not indexed yet must not appear (same as before).
    conn.execute(
        "INSERT INTO txio(txid,n,direction,address,value) VALUES(?,?,?,?,?)",
        ("f" * 64, 0, "out", A, 5),
    )
    conn.commit()


def test_address_history_matches_previous_query(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    _seed(db)
    q = Queries(db)
    for addr in (A, B, "Xnobody"):
        for limit in (1, 25, 50, 100):
            got = q.address(addr, limit)["txs"]
            want = [
                dict(r)
                for r in db.conn.execute(OLD_TX_SQL, (addr, addr, addr, min(limit, 100))).fetchall()
            ]
            assert got == want, (addr, limit)
            assert all(list(g) == list(w) for g, w in zip(got, want))
    assert q.address("Xnobody")["txs"] == []
    db.close()


def test_address_history_does_not_scan_every_tx(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    _seed(db)
    db.conn.execute("ANALYZE")
    import inspect

    src = inspect.getsource(Queries.address)
    sql = src[src.index("WITH io AS") : src.index('""",', src.index("WITH io AS"))]
    plan = [
        r["detail"]
        for r in db.conn.execute("EXPLAIN QUERY PLAN " + sql, (A, 50)).fetchall()
    ]
    text = " | ".join(plan)
    assert "idx_txio_addr" in text, text
    assert "SCAN t" not in text, text
    db.close()


def test_stats_cache_serves_fresh_and_stale_once():
    now = [0.0]
    calls = []
    cache = StaleWhileRefreshCache(ttl=30, max_stale=600, clock=lambda: now[0])

    def compute():
        calls.append(now[0])
        return {"n": len(calls)}

    assert cache.get("k", compute) == {"n": 1}
    now[0] = 10
    assert cache.get("k", compute) == {"n": 1}
    now[0] = 31
    assert cache.get("k", compute) == {"n": 2}
    assert len(calls) == 2


def test_stats_cache_one_refresh_while_others_get_stale():
    now = [0.0]
    started, release = threading.Event(), threading.Event()
    calls = []
    cache = StaleWhileRefreshCache(ttl=30, max_stale=600, clock=lambda: now[0])
    cache.get("k", lambda: "old")
    now[0] = 40

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
        return "new"

    out = {}
    t = threading.Thread(target=lambda: out.setdefault("a", cache.get("k", slow)))
    t.start()
    assert started.wait(5)
    # While the refresh runs, other callers get the previous answer at once.
    t0 = time.monotonic()
    assert cache.get("k", slow) == "old"
    assert time.monotonic() - t0 < 1
    release.set()
    t.join(5)
    assert out["a"] == "new"
    assert cache.get("k", slow) == "new"
    assert len(calls) == 1


def test_stats_cache_keeps_last_value_on_error():
    now = [0.0]
    cache = StaleWhileRefreshCache(ttl=30, max_stale=600, clock=lambda: now[0])
    cache.get("k", lambda: "good")
    now[0] = 45

    def boom():
        raise RuntimeError("db busy")

    assert cache.get("k", boom) == "good"
    fresh = StaleWhileRefreshCache()
    try:
        fresh.get("k", boom)
    except RuntimeError:
        pass
    else:
        raise AssertionError("first failure must surface")


def test_api_stats_is_cached_and_releases_connections(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    rpc = XCoinRPC(Settings())
    q = Queries(db)
    calls = []
    real = q.chain_stats

    def counted(pulse=180):
        calls.append(pulse)
        return real(pulse)

    q.chain_stats = counted
    app = create_app(q, Indexer(db, rpc), rpc)
    client = TestClient(app)
    first = client.get("/api/stats?pulse=240")
    assert first.status_code == 200
    assert "handles" in first.json() and "hat_pulse" in first.json()
    for _ in range(5):
        assert client.get("/api/stats?pulse=240").json() == first.json()
    assert calls == [240]
    # Out-of-range and junk values share the clamped keys.
    client.get("/api/stats?pulse=99999")
    client.get("/api/stats?pulse=720")
    assert calls == [240, 720]
    assert client.get("/api/stats?pulse=abc").status_code == 422
    # Finished requests keep no database files open (PR #17/#18 behaviour).
    assert db.open_connection_count() <= 1
    db.close()
