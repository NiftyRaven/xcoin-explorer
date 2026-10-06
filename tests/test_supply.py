"""Public supply endpoints for listing sites."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.chain import BURN_ADDRESSES_MAIN, COIN, lifetime_supply
from explorer.config import Settings
from explorer.db import Database
from explorer.indexer import Indexer
from explorer.queries import Queries
from explorer.rpc import XCoinRPC
from explorer.supply import (
    BURN_ADDRESSES,
    SupplyCache,
    compute_supply,
    format_xfer,
)

SUB_BURN = "XissueSubAssetXXXXXXXXXXXXXXcHkFpF"
UNIQUE_BURN = "XissueUniqueAssetXXXXXXXXXXXagKZDZ"


def _app(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    rpc = XCoinRPC(Settings())
    app = create_app(Queries(db), Indexer(db, rpc), rpc)
    return db, app


def _blocks(db: Database, tip: int) -> None:
    db.conn.executemany(
        "INSERT INTO blocks(height, hash, time, tx_count) VALUES(?,?,?,1)",
        [(h, f"{h:064x}", 1_789_197_360 + 60 * h) for h in range(tip + 1)],
    )


def _utxo(db: Database, n: int, address: str, value: int, asset: str | None = None, amount: int = 0) -> None:
    db.conn.execute(
        "INSERT INTO utxos(txid, n, address, value, asset, asset_amount, height) VALUES(?,?,?,?,?,?,1)",
        ("ab" * 32, n, address, value, asset, amount),
    )


def _db_fds(path: Path) -> int:
    root = str(path.resolve())
    count = 0
    for entry in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{entry}")
        except OSError:
            continue
        if target == root or target.startswith(root + "-"):
            count += 1
    return count


def test_format_xfer_is_exact_with_no_trailing_zeros():
    assert format_xfer(0) == "0"
    assert format_xfer(17_746_500_000_000_000) == "177465000"
    assert format_xfer(17_746_500_012_345_678) == "177465000.12345678"
    assert format_xfer(17_746_500_010_000_000) == "177465000.1"
    assert format_xfer(1) == "0.00000001"
    assert format_xfer(12_345) == "0.00012345"
    assert format_xfer(2_099_999_499_972_700_000) == "20999994999.727"
    # Past float precision: a float would round the last digits away.
    assert format_xfer(2_099_999_999_999_999_999) == "20999999999.99999999"
    assert format_xfer(-150_000_000) == "-1.5"


def test_burn_list_matches_chainparams_and_covers_explorer_labels():
    main = BURN_ADDRESSES["main"]
    assert len(main) == 10
    assert set(BURN_ADDRESSES_MAIN) <= set(main)
    assert all(a.startswith("X") and len(a) == 34 for a in main)
    assert all(a.startswith("y") and len(a) == 34 for a in BURN_ADDRESSES["regtest"])


def test_compute_supply_subtracts_only_burn_addresses(tmp_path: Path):
    db, _app_ = _app(tmp_path)
    _blocks(db, 35_493)
    _utxo(db, 0, SUB_BURN, 3_500 * COIN)
    _utxo(db, 1, UNIQUE_BURN, 20 * COIN)
    _utxo(db, 2, UNIQUE_BURN, 0, asset="CAT#1", amount=COIN)
    _utxo(db, 3, "XsomeOrdinaryHolderXXXXXXXXXXXXXXX", 10_000 * COIN)
    db.commit()
    s = compute_supply(db)
    issued = 35_493 * 5_000 * COIN
    assert s["height"] == 35_493
    assert s["issued_atoms"] == issued == 17_746_500_000_000_000
    assert s["burned_atoms"] == 3_520 * COIN
    assert s["total_atoms"] == issued - 3_520 * COIN
    assert s["circulating_atoms"] == s["total_atoms"]
    assert s["locked_atoms"] == 0
    assert s["total"] == "177461480"
    assert s["circulating"] == "177461480"
    assert s["max_atoms"] == lifetime_supply() == 2_099_999_499_972_700_000
    assert s["max"] == "20999994999.727"
    assert [b["address"] for b in s["burns"]] == [SUB_BURN, UNIQUE_BURN]
    assert s["burns"][0]["xfer"] == "3500"
    db.close()


def test_empty_index_reports_zero(tmp_path: Path):
    db, _app_ = _app(tmp_path)
    s = compute_supply(db)
    assert s["height"] == 0
    assert s["total"] == "0" and s["circulating"] == "0"
    db.close()


def test_plain_text_endpoints(tmp_path: Path):
    db, app = _app(tmp_path)
    _blocks(db, 10)
    _utxo(db, 0, SUB_BURN, 100 * COIN + 12_345)
    db.commit()
    client = TestClient(app)
    expected = {
        "/api/supply/circulating": "49899.99987655",
        "/api/supply/total": "49899.99987655",
        "/api/supply/max": "20999994999.727",
    }
    for path, body in expected.items():
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.text == body
        assert r.headers["content-type"].startswith("text/plain")
        assert r.headers["access-control-allow-origin"] == "*"
        assert r.headers["cache-control"] == "public, max-age=60"
        cors = client.get(path, headers={"Origin": "https://blockspot.io"})
        assert cors.headers["access-control-allow-origin"] == "*"
        head = client.head(path)
        assert head.status_code == 200
        assert head.headers["content-type"].startswith("text/plain")
    db.close()


def test_json_endpoint(tmp_path: Path):
    db, app = _app(tmp_path)
    _blocks(db, 3)
    db.commit()
    r = TestClient(app).get("/api/supply")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["access-control-allow-origin"] == "*"
    body = r.json()
    assert body["height"] == 3
    assert body["circulating"] == body["total"] == "15000"
    assert body["max"] == "20999994999.727"
    assert body["total_atoms"] == 15_000 * COIN
    assert body["burns"] == []
    db.close()


def test_supply_is_cached_for_a_minute(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    _blocks(db, 2)
    db.commit()
    now = [1000.0]
    cache = SupplyCache(db, ttl=60, clock=lambda: now[0])
    assert cache.get()["total"] == "10000"
    db.conn.execute("INSERT INTO blocks(height, hash, time, tx_count) VALUES(3, ?, 0, 1)", ("ff" * 32,))
    db.commit()
    now[0] += 59
    assert cache.get()["total"] == "10000"
    now[0] += 1
    assert cache.get()["total"] == "15000"
    db.close()


def test_cache_keeps_last_good_value_after_a_read_error(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "t.db")
    _blocks(db, 1)
    db.commit()
    now = [0.0]
    cache = SupplyCache(db, ttl=60, clock=lambda: now[0])
    assert cache.get()["total"] == "5000"

    def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(db, "indexed_height", boom)
    now[0] += 120
    assert cache.get()["total"] == "5000"
    db.close()


def test_supply_requests_do_not_keep_connections(tmp_path: Path):
    db, app = _app(tmp_path)
    _blocks(db, 5)
    db.commit()
    app.state.supply = SupplyCache(db, ttl=0)  # hit the database every request
    before_conns = db.open_connection_count()
    before_fds = _db_fds(db.path)
    client = TestClient(app)
    for _ in range(50):
        for path in ("/api/supply", "/api/supply/circulating", "/api/supply/total", "/api/supply/max"):
            r = client.get(path)
            assert r.status_code == 200
            assert r.content
    assert db.open_connection_count() <= before_conns
    assert _db_fds(db.path) - before_fds < 8
    assert client.get("/api/status").status_code == 200
    db.close()
    assert db.open_connection_count() == 0
    assert _db_fds(db.path) == 0


def test_unknown_supply_path_is_404(tmp_path: Path):
    db, app = _app(tmp_path)
    r = TestClient(app).get("/api/supply/nope")
    assert r.status_code == 404
    db.close()
