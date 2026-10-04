"""Database calls must not wedge the explorer API."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.config import Settings
from explorer.db import Database
from explorer.indexer import Indexer
from explorer.queries import Queries, row_to_dict
from explorer.rpc import XCoinRPC


class _BadRow:
    def keys(self):
        return ["v"]

    def __getitem__(self, key):
        raise IndexError("tuple index out of range")

    def __iter__(self):
        raise IndexError("tuple index out of range")


class _Cursor:
    def __init__(self, one=None, rows=()):
        self._one = one
        self._rows = list(rows)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._rows)


class _MissingConn:
    """Aggregate reads come back empty, the way a raced cursor did."""

    def execute(self, sql, params=()):
        compact = " ".join(sql.split())
        if "AS v FROM utxos" in compact:
            return _Cursor(None)
        if "AS v FROM txio" in compact and "direction='out'" in compact:
            return _Cursor(None)
        if "AS v FROM txio" in compact and "direction='in'" in compact:
            return _Cursor(_BadRow())
        if "lottery_share_guests" in compact:
            return _Cursor(rows=[_BadRow(), None])
        if "GROUP BY asset" in compact:
            return _Cursor(rows=[_BadRow()])
        return _Cursor(None)


class _MissingDB:
    conn = _MissingConn()


def test_row_to_dict_missing_row_does_not_throw():
    assert row_to_dict(None) == {}
    assert row_to_dict(_BadRow()) == {}

    class _NoneItem:
        def keys(self):
            return ["v"]

        def __getitem__(self, key):
            raise TypeError("'NoneType' object is not subscriptable")

    assert row_to_dict(_NoneItem()) == {}


def test_address_missing_row_does_not_throw():
    body = Queries(_MissingDB()).address("Xmissing")
    assert body["address"] == "Xmissing"
    assert body["balance_atoms"] == 0
    assert body["received_atoms"] == 0
    assert body["sent_atoms"] == 0
    assert body["assets"] == []
    assert body["txs"] == []
    assert body["lottery_wins"] == []
    assert body["guest_shares"] == []
    assert body["identity"] is None


def test_each_thread_has_its_own_connection(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    ids = []

    def grab():
        ids.append(id(db._thread_conn()))

    grab()
    thread = threading.Thread(target=grab)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(ids) == 2
    assert ids[0] != ids[1]
    db.close()


def test_uncommitted_write_is_not_shared_and_commit_is_visible(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    started = threading.Event()
    release = threading.Event()

    def writer():
        db.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?)", ("best_hash", "abc"))
        started.set()
        assert release.wait(5)
        db.commit()

    thread = threading.Thread(target=writer)
    thread.start()
    assert started.wait(5)
    assert db.get_meta("best_hash") is None
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert db.get_meta("best_hash") == "abc"
    db.close()


def test_interface_error_is_replaced_for_the_next_read(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    db.set_meta("best_hash", "abc")
    db.commit()
    poisoned: set[int] = set()

    class _Conn(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if id(self) in poisoned and isinstance(sql, str) and "FROM meta" in sql:
                raise sqlite3.InterfaceError("bad parameter or other API misuse")
            return super().execute(sql, parameters)

    def open_conn():
        conn = sqlite3.connect(str(db.path), timeout=5.0, check_same_thread=False, factory=_Conn)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000").fetchone()
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("PRAGMA synchronous=NORMAL").fetchone()
        conn.execute("PRAGMA foreign_keys=ON").fetchone()
        with db._guard:
            db._conns.append(conn)
        return conn

    db.discard_thread_connection()
    db._open = open_conn
    poisoned.add(id(db._thread_conn()))
    assert db.get_meta("best_hash") == "abc"
    assert id(db._thread_conn()) not in poisoned
    status = Queries(db).status({"tip": 3, "indexing": False}, True, "")
    assert status["best_hash"] == "abc"
    assert status["indexed_height"] == -1
    db.close()


def test_closed_connection_does_not_stick(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    db.set_meta("best_hash", "abc")
    db.commit()
    db._thread_conn().close()
    assert db.get_meta("best_hash") == "abc"
    assert db.indexed_height() == -1
    db.close()


def test_writer_and_readers_do_not_wedge(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    db.set_meta("best_hash", "start")
    db.commit()
    queries = Queries(db)
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer():
        i = 0
        while not stop.is_set():
            try:
                db.set_meta("best_hash", f"h{i}")
                db.conn.execute(
                    "INSERT OR REPLACE INTO blocks(height, hash, time, tx_count) VALUES(?,?,?,?)",
                    (i % 20, f"{i % 20:064x}", 1_700_000_000 + i, 0),
                )
                db.commit()
                i += 1
            except Exception as exc:
                errors.append(exc)
                return

    def reader():
        while not stop.is_set():
            try:
                body = queries.address("X" + "a" * 20)
                assert body["balance_atoms"] == 0
                assert db.get_meta("best_hash")
                db.indexed_height()
                queries.status({"tip": 0, "indexing": False}, False, "")
            except Exception as exc:
                errors.append(exc)
                return

    threads = [threading.Thread(target=writer, name="writer")]
    threads += [threading.Thread(target=reader, name=f"reader-{n}") for n in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(0.4)
    stop.set()
    for thread in threads:
        thread.join(timeout=8)
    alive = [thread.name for thread in threads if thread.is_alive()]
    assert alive == []
    assert errors == []
    assert db.get_meta("best_hash")
    tip = queries.status({"tip": 1, "indexing": False}, True, "")
    assert tip["indexed_height"] >= 0
    db.close()


def _app(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    rpc = XCoinRPC(Settings())
    indexer = Indexer(db, rpc)
    queries = Queries(db)
    app = create_app(queries, indexer, rpc)
    return db, queries, app


def test_healthy_pages_still_answer(tmp_path: Path):
    db, _queries, app = _app(tmp_path)
    txid = "cd" * 32
    db.conn.execute(
        "INSERT INTO blocks(height, hash, time, tx_count) VALUES(?,?,?,?)",
        (1, "ab" * 32, 1_700_000_000, 1),
    )
    db.conn.execute(
        "INSERT INTO txs(txid, height, n, time) VALUES(?,?,?,?)",
        (txid, 1, 0, 1_700_000_000),
    )
    db.conn.execute(
        "INSERT INTO utxos(txid, n, address, value, height) VALUES(?,?,?,?,?)",
        (txid, 0, "Xhealthy", 25, 1),
    )
    db.commit()
    client = TestClient(app)
    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["indexed_height"] == 1
    tip = client.get("/api/tip")
    assert tip.status_code == 200
    assert tip.json()["indexed_height"] == 1
    assert client.get("/api/block/1").status_code == 200
    assert client.get(f"/api/tx/{txid}").status_code == 200
    found = client.get("/api/search", params={"q": "1"})
    assert found.status_code == 200
    assert any(item["type"] == "block" for item in found.json()["results"])
    addr = client.get("/api/address/Xhealthy")
    assert addr.status_code == 200
    assert addr.json()["balance_atoms"] == 25
    missing = client.get("/api/address/Xmissing")
    assert missing.status_code == 200
    assert missing.json()["balance_atoms"] == 0
    assert missing.json()["txs"] == []
    home = client.get("/")
    assert home.status_code == 200
    db.close()


def test_address_exception_returns_404_and_status_still_answers(tmp_path: Path, monkeypatch):
    db, queries, app = _app(tmp_path)
    db.set_meta("best_hash", "abc")
    db.commit()
    client = TestClient(app)

    def raise_index(_addr, limit=50):
        raise IndexError("tuple index out of range")

    monkeypatch.setattr(queries, "address", raise_index)
    missed = client.get("/api/address/Xmissing")
    assert missed.status_code == 404
    assert missed.json()["error"] == "address not found"
    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["best_hash"] == "abc"
    tip = client.get("/api/tip")
    assert tip.status_code == 200
    assert "indexed_height" in tip.json()

    def raise_type(_addr, limit=50):
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(queries, "address", raise_type)
    missed = client.get("/api/address/Xmissing")
    assert missed.status_code == 404
    status = client.get("/api/status")
    assert status.status_code == 200
    tip = client.get("/api/tip")
    assert tip.status_code == 200
    assert tip.json()["hash"] == "abc"
    db.close()
