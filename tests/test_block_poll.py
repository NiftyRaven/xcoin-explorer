"""Indexer / settings poll cadence follows X Coin block time."""

from pathlib import Path

from explorer.chain import BLOCK_TIME_SECONDS
from explorer.config import Settings
from explorer.db import Database
from explorer.indexer import Indexer
from explorer.rpc import XCoinRPC


def test_default_poll_seconds_is_block_time():
    assert Settings().poll_seconds == float(BLOCK_TIME_SECONDS)
    assert BLOCK_TIME_SECONDS == 60


def test_indexer_sleeps_block_time_when_synced(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    indexer = Indexer(db, XCoinRPC(Settings()))
    indexer.status["indexing"] = False
    now = 1_789_197_400.0
    db.conn.execute(
        "INSERT INTO blocks(height, hash, time) VALUES(?, ?, ?)",
        (1, "ab" * 32, int(now - 15)),
    )
    db.commit()
    delay = indexer._next_poll_delay()
    assert 0 < delay <= BLOCK_TIME_SECONDS
    indexer.status["indexing"] = True
    assert indexer._next_poll_delay() == 0.0
    db.close()
