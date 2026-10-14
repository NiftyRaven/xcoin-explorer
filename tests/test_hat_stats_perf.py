"""Hat stats stay fast on a production-sized hat table.

About 1.25 million lottery_active rows (50,000 draws, 25 nodes each). A handle
that was in every draw, on three node ids, must come back in under half a
second the first time. A name that is not in the index must 404 in about 0 ms,
before any aggregate runs.
"""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.db import Database
from explorer.hatstats import compute_hat_stats, indexed_lookup_plans
from explorer.indexer import Indexer
from explorer.queries import Queries
from explorer.rpc import XCoinRPC
from explorer.config import Settings

ROWS = 1_250_000
BLOCKS = 50_000
NOW = 1_700_000_000 + BLOCKS * 60 + 10


def test_hat_stats_large_table_is_fast_uncached(tmp_path: Path):
    db = Database(tmp_path / "hat-perf.db")
    plans = indexed_lookup_plans(db.conn)
    assert "idx_active_xaccount_norm" in plans["active_handle"]
    assert "SCAN lottery_active" not in plans["active_handle"]
    assert "idx_wins_xaccount_norm" in plans["wins_handle"]
    assert "SCAN lottery_wins" not in plans["wins_handle"]
    assert "idx_active_node_lower" in plans["active_node"]
    assert "SCAN lottery_active" not in plans["active_node"]

    db.conn.execute(
        f"""
        WITH RECURSIVE seq(n) AS (SELECT 0 UNION ALL SELECT n+1 FROM seq WHERE n<{ROWS - 1})
        INSERT INTO lottery_active(height, node_id, xaccount, n)
        SELECT (n/25)+1,
               CASE
                 WHEN n%25=0 THEN '4efdae22aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
                 WHEN n%25=1 THEN 'f4529136bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
                 WHEN n%25=2 THEN 'd366d9f5cccccccccccccccccccccccccccccccc'
                 ELSE printf('ee%038x', n%25)
               END,
               CASE
                 WHEN n%25=0 THEN 'zekedrops'
                 WHEN n%25=1 THEN 'zekedrops'
                 WHEN n%25=2 THEN NULL
                 ELSE 'h' || (n%25)
               END,
               n%25
        FROM seq
        """
    )
    db.conn.execute(
        f"""
        WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<{BLOCKS})
        INSERT INTO blocks(height, hash, time, tx_count, active_count, winner_count)
        SELECT n, printf('%064x', n), {1_700_000_000}+n*60, 1, 25, 1 FROM seq
        """
    )
    db.conn.execute(
        f"""
        WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<{BLOCKS})
        INSERT INTO lottery_wins(height, rank, node_id, address, amount, xaccount, is_producer)
        SELECT n, 0,
               CASE WHEN n%30=0 THEN '4efdae22aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ELSE printf('ee%038x', 7) END,
               CASE WHEN n%30=0 THEN 'Xzekepay' ELSE 'Xother' END,
               500000000000,
               CASE WHEN n%30=0 THEN 'zekedrops' ELSE 'h7' END,
               1
        FROM seq
        """
    )
    db.conn.execute(
        "INSERT INTO identities(handle, node_id) VALUES('zekedrops','d366d9f5cccccccccccccccccccccccccccccccc')"
    )
    db.commit()

    started = time.perf_counter()
    stats = compute_hat_stats(db, "zekedrops", now=NOW)
    elapsed = time.perf_counter() - started
    assert stats is not None
    assert stats["draws_all"] == BLOCKS
    assert stats["wins"] == BLOCKS // 30
    assert len(stats["nodes"]) == 3
    assert stats["expected_wins"] == 2000.0
    assert elapsed < 0.5, f"uncached real handle took {elapsed:.3f}s"

    rpc = XCoinRPC(Settings())
    client = TestClient(create_app(Queries(db), Indexer(db, rpc), rpc))
    started = time.perf_counter()
    missing = client.get("/api/handle/not_a_real_handle/hat-stats")
    unknown = time.perf_counter() - started
    assert missing.status_code == 404
    assert missing.json()["error"] == "not found"
    assert unknown < 0.05, f"unknown handle took {unknown * 1000:.1f}ms"
    db.close()
