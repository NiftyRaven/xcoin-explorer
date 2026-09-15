"""1.0.14 host/guest share math and indexer tagging."""

from pathlib import Path

from explorer.config import Settings
from explorer.db import Database
from explorer.hostshare import detect_host_share, guest_pot, guest_share
from explorer.indexer import SHARE_INDEX_V, Indexer
from explorer.queries import Queries
from explorer.rpc import XCoinRPC

COIN = 100_000_000


def test_guest_pot_matches_wallet():
    assert guest_pot(1250 * COIN, 20) == 250 * COIN
    assert guest_share(250 * COIN, 1) == 250 * COIN
    assert guest_share(250 * COIN, 2) == 125 * COIN
    assert guest_pot(5000 * COIN, 20) == 1000 * COIN
    assert guest_share(guest_pot(5000 * COIN, 20), 2) == 500 * COIN
    assert guest_pot(0, 20) == 0
    assert guest_pot(1250 * COIN, 0) == 0
    assert guest_share(250 * COIN, 0) == 0
    assert guest_pot(1250 * COIN, 100) == 1250 * COIN
    # After first ½: guests get % of the host slice, not the whole subsidy.
    assert guest_pot(1250 * COIN, 20) != guest_pot(2500 * COIN, 20)
    assert guest_pot(1250 * COIN, 20) != guest_pot(5000 * COIN, 20)


def test_detect_equal_split():
    host = "Xhostaddr11111111111111111111111111"
    guest_a = "XguestA2222222222222222222222222222"
    guest_b = "XguestB3333333333333333333333333333"
    win = 5000 * COIN
    pot = guest_pot(win, 20)
    each = guest_share(pot, 2)
    found = detect_host_share(
        win,
        host,
        [
            (guest_a, each),
            (guest_b, each),
            (host, win - pot - 10000),
        ],
    )
    assert found is not None
    assert found["percent"] == 20
    assert found["pot"] == pot
    assert len(found["guests"]) == 2
    assert detect_host_share(win, host, [(guest_a, 123), (host, win - 123)]) is None


def test_index_share_from_mature_win(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    indexer = Indexer(db, XCoinRPC(Settings()))
    host = "Xhostaddr11111111111111111111111111"
    guest = "XguestA2222222222222222222222222222"
    cb = "aa" * 32
    share_txid = "bb" * 32
    win = 5000 * COIN
    pot = guest_pot(win, 20)
    db.conn.execute(
        """
        INSERT INTO blocks(height, hash, prev, time, nonce, size, tx_count, producer, subsidy)
        VALUES(10, ?, NULL, 0, 0, 0, 1, NULL, ?)
        """,
        ("cc" * 32, win),
    )
    db.conn.execute(
        """
        INSERT INTO txs(txid, height, n, time, coinbase, identity, xid_handle, xfer_in, xfer_out, fee, vin_count, vout_count)
        VALUES(?, 10, 0, 0, 1, 0, NULL, 0, ?, 0, 1, 1)
        """,
        (cb, win),
    )
    db.conn.execute(
        """
        INSERT INTO lottery_wins(height, rank, node_id, address, amount, xaccount, is_producer)
        VALUES(10, 0, 'id', ?, ?, 'humble_miner', 1)
        """,
        (host, win),
    )
    db.conn.execute(
        """
        INSERT INTO identities(handle, asset, address, node_id, first_height, txid)
        VALUES('lampacenter', 'LAMPACENTER', ?, NULL, 1, 'dd')
        """,
        (guest,),
    )
    db.conn.execute(
        """
        INSERT INTO txs(txid, height, n, time, coinbase, identity, xid_handle, xfer_in, xfer_out, fee, vin_count, vout_count)
        VALUES(?, 120, 1, 0, 0, 0, NULL, ?, ?, 10000, 1, 2)
        """,
        (share_txid, win, pot + (win - pot - 10000)),
    )
    db.conn.execute(
        """
        INSERT INTO txio(txid, n, direction, address, value, spent_txid, spent_n, coinbase, script_type)
        VALUES(?, 0, 'in', ?, ?, ?, 0, 0, 'script')
        """,
        (share_txid, host, win, cb),
    )
    db.conn.execute(
        """
        INSERT INTO txio(txid, n, direction, address, value, coinbase, script_type)
        VALUES(?, 0, 'out', ?, ?, 0, 'pubkeyhash')
        """,
        (share_txid, guest, pot),
    )
    db.conn.execute(
        """
        INSERT INTO txio(txid, n, direction, address, value, coinbase, script_type)
        VALUES(?, 1, 'out', ?, ?, 0, 'pubkeyhash')
        """,
        (share_txid, host, win - pot - 10000),
    )
    db.commit()
    indexer._maybe_index_share(share_txid)
    db.commit()
    q = Queries(db)
    tx = q.tx(share_txid)
    assert tx["host_share"]["guest_percent"] == 20
    assert tx["host_share"]["host_handle"] == "humble_miner"
    assert tx["host_share"]["guests"][0]["handle"] == "lampacenter"
    profile = q.handle_profile("humble_miner")
    assert profile["shares_sent"][0]["txid"] == share_txid
    guest_p = q.handle_profile("lampacenter")
    assert guest_p["shares_received"][0]["host_handle"] == "humble_miner"
    addr = q.address(guest)
    assert addr["guest_shares"][0]["host_handle"] == "humble_miner"
    assert q.recent_shares(5)[0]["txid"] == share_txid
    db.close()


def test_share_index_v_constant():
    assert SHARE_INDEX_V == "1"
