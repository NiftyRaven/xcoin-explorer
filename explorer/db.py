"""SQLite index for the explorer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    height INTEGER PRIMARY KEY,
    hash TEXT NOT NULL UNIQUE,
    prev TEXT,
    time INTEGER,
    nonce INTEGER,
    size INTEGER,
    tx_count INTEGER,
    producer TEXT,
    subsidy INTEGER DEFAULT 0,
    fees INTEGER DEFAULT 0,
    lottery_slot INTEGER,
    lottery_seed TEXT,
    winner_count INTEGER DEFAULT 0,
    active_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_blocks_time ON blocks(time);
CREATE INDEX IF NOT EXISTS idx_blocks_producer ON blocks(producer);

CREATE TABLE IF NOT EXISTS txs (
    txid TEXT PRIMARY KEY,
    height INTEGER,
    n INTEGER,
    time INTEGER,
    coinbase INTEGER DEFAULT 0,
    identity INTEGER DEFAULT 0,
    xid_handle TEXT,
    xfer_in INTEGER DEFAULT 0,
    xfer_out INTEGER DEFAULT 0,
    fee INTEGER DEFAULT 0,
    vin_count INTEGER DEFAULT 0,
    vout_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_txs_height ON txs(height, n);
CREATE INDEX IF NOT EXISTS idx_txs_handle ON txs(xid_handle);

CREATE TABLE IF NOT EXISTS txio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txid TEXT NOT NULL,
    n INTEGER NOT NULL,
    direction TEXT NOT NULL, -- in | out
    address TEXT,
    value INTEGER DEFAULT 0,
    asset TEXT,
    asset_amount INTEGER DEFAULT 0,
    asset_kind TEXT,
    spent_txid TEXT,
    spent_n INTEGER,
    coinbase INTEGER DEFAULT 0,
    script_type TEXT,
    UNIQUE(txid, n, direction)
);
CREATE INDEX IF NOT EXISTS idx_txio_addr ON txio(address, direction);
CREATE INDEX IF NOT EXISTS idx_txio_asset ON txio(asset);
CREATE INDEX IF NOT EXISTS idx_txio_spent ON txio(spent_txid, spent_n);

CREATE TABLE IF NOT EXISTS utxos (
    txid TEXT NOT NULL,
    n INTEGER NOT NULL,
    address TEXT,
    value INTEGER DEFAULT 0,
    asset TEXT,
    asset_amount INTEGER DEFAULT 0,
    height INTEGER,
    PRIMARY KEY (txid, n)
);
CREATE INDEX IF NOT EXISTS idx_utxo_addr ON utxos(address);
CREATE INDEX IF NOT EXISTS idx_utxo_asset ON utxos(asset);

CREATE TABLE IF NOT EXISTS lottery_wins (
    height INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    node_id TEXT,
    address TEXT,
    amount INTEGER,
    xaccount TEXT,
    is_producer INTEGER DEFAULT 0,
    PRIMARY KEY (height, rank)
);
CREATE INDEX IF NOT EXISTS idx_wins_addr ON lottery_wins(address);
CREATE INDEX IF NOT EXISTS idx_wins_handle ON lottery_wins(xaccount);
CREATE INDEX IF NOT EXISTS idx_wins_node ON lottery_wins(node_id);

CREATE TABLE IF NOT EXISTS lottery_active (
    height INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    PRIMARY KEY (height, node_id)
);

CREATE TABLE IF NOT EXISTS assets (
    name TEXT PRIMARY KEY,
    kind TEXT,
    amount INTEGER DEFAULT 0,
    units INTEGER DEFAULT 0,
    reissuable INTEGER DEFAULT 0,
    ipfs TEXT,
    created_height INTEGER,
    created_txid TEXT,
    issuer TEXT,
    x_handle TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(kind);
CREATE INDEX IF NOT EXISTS idx_assets_handle ON assets(x_handle);

CREATE TABLE IF NOT EXISTS asset_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    height INTEGER,
    txid TEXT,
    name TEXT,
    kind TEXT,
    amount INTEGER,
    address TEXT
);
CREATE INDEX IF NOT EXISTS idx_aa_name ON asset_activity(name, id DESC);
CREATE INDEX IF NOT EXISTS idx_aa_height ON asset_activity(height DESC);

CREATE TABLE IF NOT EXISTS identities (
    handle TEXT PRIMARY KEY,
    asset TEXT,
    address TEXT,
    node_id TEXT,
    first_height INTEGER,
    txid TEXT
);
CREATE INDEX IF NOT EXISTS idx_ident_asset ON identities(asset);
CREATE INDEX IF NOT EXISTS idx_ident_addr ON identities(address);
CREATE INDEX IF NOT EXISTS idx_ident_node ON identities(node_id);

CREATE TABLE IF NOT EXISTS node_seen (
    node_id TEXT PRIMARY KEY,
    script TEXT,
    address TEXT,
    xaccount TEXT,
    last_height INTEGER,
    last_seen INTEGER
);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def indexed_height(self) -> int:
        row = self.conn.execute("SELECT MAX(height) AS h FROM blocks").fetchone()
        return int(row["h"] if row and row["h"] is not None else -1)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()
