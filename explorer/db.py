"""SQLite index for the explorer."""

from __future__ import annotations

import sqlite3
import threading
import weakref
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
    op_return TEXT,
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
    xaccount TEXT,
    n INTEGER DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS lottery_shares (
    txid TEXT PRIMARY KEY,
    height INTEGER,
    host_txid TEXT,
    host_height INTEGER,
    host_handle TEXT,
    host_address TEXT,
    win_amount INTEGER,
    pot_amount INTEGER,
    guest_percent INTEGER,
    guest_count INTEGER,
    guest_each INTEGER
);
CREATE INDEX IF NOT EXISTS idx_shares_host ON lottery_shares(host_handle);
CREATE INDEX IF NOT EXISTS idx_shares_height ON lottery_shares(height DESC);

CREATE TABLE IF NOT EXISTS lottery_share_guests (
    txid TEXT NOT NULL,
    n INTEGER NOT NULL,
    address TEXT,
    handle TEXT,
    amount INTEGER,
    PRIMARY KEY (txid, n)
);
CREATE INDEX IF NOT EXISTS idx_share_guest_handle ON lottery_share_guests(handle);
CREATE INDEX IF NOT EXISTS idx_share_guest_addr ON lottery_share_guests(address);

-- First verified reserve for a listing. A later buy does not replace it.
CREATE TABLE IF NOT EXISTS launch_reserves (
    asset TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    txid TEXT
);

-- Launch platform fee addresses, learned from verified XL1 sells.
CREATE TABLE IF NOT EXISTS launch_fee_addresses (
    address TEXT PRIMARY KEY,
    txid TEXT
);
"""


# Reads are safe to run again on a fresh connection. Writes are not.
_READ_PREFIXES = ("SELECT", "PRAGMA", "WITH", "EXPLAIN")


def _is_read(sql: str) -> bool:
    text = sql.lstrip()
    if not text:
        return False
    head = text.split(None, 1)[0].upper()
    return head in _READ_PREFIXES


def _recoverable(exc: BaseException) -> bool:
    """Errors that mean this connection must not be used again."""
    if isinstance(exc, sqlite3.InterfaceError):
        return True
    if isinstance(exc, sqlite3.ProgrammingError) and "closed" in str(exc).lower():
        return True
    # Raised by sqlite3.connect when the process cannot open another file.
    # Retry once on a new connection instead of failing the request for good.
    return (
        isinstance(exc, sqlite3.OperationalError)
        and "unable to open database file" in str(exc).lower()
    )


def _row_value(row, key: str, default=None):
    if row is None:
        return default
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


class _Cursor:
    """Cursor that drops a broken connection instead of reusing it."""

    def __init__(self, db: Database, sql: str, parameters):
        self._db = db
        self._sql = sql
        self._parameters = parameters
        self._retried = False
        self._cur = self._start()

    def _start(self):
        try:
            return self._db._thread_conn().execute(self._sql, self._parameters)
        except sqlite3.Error as exc:
            if not _recoverable(exc):
                raise
            return self._replace(exc)

    def _replace(self, exc: sqlite3.Error):
        self._db.discard_thread_connection()
        if self._retried or not _is_read(self._sql):
            raise exc
        self._retried = True
        try:
            return self._db._thread_conn().execute(self._sql, self._parameters)
        except sqlite3.Error as retry_exc:
            if _recoverable(retry_exc):
                self._db.discard_thread_connection()
            raise

    def _fetch(self, name: str):
        try:
            return getattr(self._cur, name)()
        except sqlite3.Error as exc:
            if not _recoverable(exc):
                raise
            self._cur = self._replace(exc)
            try:
                return getattr(self._cur, name)()
            except sqlite3.Error as retry_exc:
                if _recoverable(retry_exc):
                    self._db.discard_thread_connection()
                raise

    def fetchone(self):
        return self._fetch("fetchone")

    def fetchall(self):
        return self._fetch("fetchall")

    def __iter__(self):
        return iter(self.fetchall())


class _Owner:
    """Holds one thread's connection.

    The owner is stored only in thread-local state. When that thread ends,
    the owner is collected and its finalizer closes the database file.
    """

    def __init__(self, db: Database, conn: sqlite3.Connection):
        self.conn = conn
        self.finalizer = weakref.finalize(self, db._drop_conn, conn)


class _Conn:
    """Thread-local connection handle. `db.conn.execute(...)` stays valid."""

    def __init__(self, db: Database):
        self._db = db

    def execute(self, sql, parameters=()):
        return _Cursor(self._db, sql, parameters)

    def executemany(self, sql, seq):
        try:
            return self._db._thread_conn().executemany(sql, seq)
        except sqlite3.Error as exc:
            if _recoverable(exc):
                self._db.discard_thread_connection()
            raise

    def executescript(self, script: str):
        try:
            return self._db._thread_conn().executescript(script)
        except sqlite3.Error as exc:
            if _recoverable(exc):
                self._db.discard_thread_connection()
            raise

    def commit(self):
        self._db._thread_conn().commit()

    def rollback(self):
        try:
            self._db._thread_conn().rollback()
        except sqlite3.Error as exc:
            if _recoverable(exc):
                self._db.discard_thread_connection()
                return
            raise

    def close(self):
        self._db.discard_thread_connection()

    def __getattr__(self, name: str):
        return getattr(self._db._thread_conn(), name)


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._local = threading.local()
        self._guard = threading.Lock()
        self._conns: list[sqlite3.Connection] = []
        self._closed = False
        self._handle = _Conn(self)
        self._thread_conn().executescript(SCHEMA)
        self._migrate()

    def open_connection_count(self) -> int:
        with self._guard:
            return len(self._conns)

    @property
    def conn(self) -> _Conn:
        return self._handle

    def _open(self) -> sqlite3.Connection:
        # check_same_thread is off so process shutdown can close every
        # connection. Each connection is still used by only one thread.
        conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Finish each pragma statement. An open statement on this connection
        # would make the next call fail with "bad parameter or other API misuse".
        for pragma in (
            "PRAGMA busy_timeout=5000",
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA foreign_keys=ON",
        ):
            cur = conn.execute(pragma)
            try:
                cur.fetchone()
            finally:
                cur.close()
        with self._guard:
            self._conns.append(conn)
        return conn

    def _thread_conn(self) -> sqlite3.Connection:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        owner = getattr(self._local, "owner", None)
        if owner is not None and owner.conn is not None:
            return owner.conn
        conn = self._open()
        self._local.owner = _Owner(self, conn)
        return conn

    def discard_thread_connection(self) -> None:
        """Close this thread's connection so the next call opens a new one."""
        owner = getattr(self._local, "owner", None)
        self._local.owner = None
        if owner is None or owner.conn is None:
            return
        conn = owner.conn
        owner.conn = None
        # The finalizer would close it too. Detach so that runs once.
        if not owner.finalizer.detach():
            return
        self._drop_conn(conn)

    def _drop_conn(self, conn: sqlite3.Connection) -> None:
        with self._guard:
            try:
                self._conns.remove(conn)
            except ValueError:
                pass
        try:
            conn.close()
        except sqlite3.Error:
            pass

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(lottery_active)")}
        if "xaccount" not in cols:
            self.conn.execute("ALTER TABLE lottery_active ADD COLUMN xaccount TEXT")
        if "n" not in cols:
            self.conn.execute("ALTER TABLE lottery_active ADD COLUMN n INTEGER DEFAULT 0")
        io_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(txio)")}
        if "op_return" not in io_cols:
            self.conn.execute("ALTER TABLE txio ADD COLUMN op_return TEXT")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS launch_reserves (
                asset TEXT PRIMARY KEY,
                address TEXT NOT NULL,
                txid TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS launch_fee_addresses (
                address TEXT PRIMARY KEY,
                txid TEXT
            )
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self._closed = True
        owner = getattr(self._local, "owner", None)
        self._local.owner = None
        if owner is not None:
            owner.conn = None
            owner.finalizer.detach()
        with self._guard:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return _row_value(row, "value", default)

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def indexed_height(self) -> int:
        row = self.conn.execute("SELECT MAX(height) AS h FROM blocks").fetchone()
        value = _row_value(row, "h", None)
        return int(value) if value is not None else -1

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()
