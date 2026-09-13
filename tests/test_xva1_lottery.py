"""Height-139-style XVA1: hat handles + payout Hash160 → winner handle."""

from pathlib import Path

from explorer.db import Database
from explorer.decode import (
    address_from_script,
    coinbase_lottery,
    hash160,
    lottery_node_id_from_script_hex,
    parse_xva1,
    rpc_hex,
)
from explorer.indexer import Indexer, LOTTERY_INDEX_V
from explorer.queries import Queries
from explorer.rpc import XCoinRPC
from explorer.config import Settings


def _op_return(payload: bytes) -> bytes:
    n = len(payload)
    if n <= 75:
        return bytes([0x6A, n]) + payload
    if n <= 255:
        return bytes([0x6A, 0x4C, n]) + payload
    return bytes([0x6A, 0x4D]) + n.to_bytes(2, "little") + payload


def _xva_item(handle: str, node_id_raw: bytes, stamp: bytes) -> bytes:
    h = handle.encode("ascii")
    return bytes([len(h)]) + h + node_id_raw + stamp


def _height139_style():
    """3-handle XVA1; P2PK payout Hash160 matches humble_miner. Dummy stamps."""
    pubkey = b"\x02" + (b"\x11" * 32)
    payout = bytes([33]) + pubkey + bytes([0xAC])
    humble_raw = hash160(payout)
    nftrvn_raw = b"\x22" * 20
    lampa_raw = b"\x33" * 20
    # 65-byte compact-sig slot; not a real signature (explorer does not verify).
    stamp = b"\xaa" * 65
    xva_payload = (
        b"XVA1"
        + (3).to_bytes(4, "little")
        + _xva_item("humble_miner", humble_raw, stamp)
        + _xva_item("nftrvn", nftrvn_raw, stamp)
        + _xva_item("lampacenter", lampa_raw, stamp)
    )
    xhb_payload = (
        b"XHB1"
        + (3).to_bytes(4, "little")
        + humble_raw
        + nftrvn_raw
        + lampa_raw
    )
    xid_payload = b"XID1" + b"NFTRVN"
    vouts = [
        {
            "n": 0,
            "value": 5000.0,
            "scriptPubKey": {
                "hex": payout.hex(),
                "type": "pubkey",
                "addresses": [address_from_script(payout, "main")],
            },
        },
        {
            "n": 1,
            "value": 0.0,
            "scriptPubKey": {"hex": _op_return(xhb_payload).hex(), "type": "nulldata"},
        },
        {
            "n": 2,
            "value": 0.0,
            "scriptPubKey": {"hex": _op_return(xva_payload).hex(), "type": "nulldata"},
        },
        {
            "n": 3,
            "value": 0.0,
            "scriptPubKey": {"hex": _op_return(xid_payload).hex(), "type": "nulldata"},
        },
    ]
    return payout, humble_raw, vouts


def test_parse_xva1_three_handles():
    _payout, humble_raw, vouts = _height139_style()
    script = bytes.fromhex(vouts[2]["scriptPubKey"]["hex"])
    rows = parse_xva1(script)
    assert rows is not None
    assert [r.handle for r in rows] == ["humble_miner", "nftrvn", "lampacenter"]
    assert rows[0].node_id == rpc_hex(humble_raw)


def test_humble_miner_wins_height139_style():
    payout, humble_raw, vouts = _height139_style()
    assert lottery_node_id_from_script_hex(payout.hex()) == rpc_hex(humble_raw)
    lot = coinbase_lottery(vouts, "main")
    assert lot["handles"] == ["humble_miner", "nftrvn", "lampacenter"]
    assert lot["xid1"] == "nftrvn"  # parsed, but not lottery identity
    assert lot["winner_handle"] == "humble_miner"
    assert len(lot["winners"]) == 1
    assert lot["winners"][0]["xaccount"] == "humble_miner"
    assert lot["winners"][0]["amount"] == 5000 * 100_000_000
    assert lot["id_to_handle"][rpc_hex(humble_raw)] == "humble_miner"


def test_xid1_is_not_lottery_identity(tmp_path: Path):
    payout, _humble_raw, vouts = _height139_style()
    db = Database(tmp_path / "t.db")
    indexer = Indexer(db, XCoinRPC(Settings()))
    txid = "ec6096f48a506e0c0978a691bb2b75ace5462df5e2ee0c2b1c0bbc684dd81ad3"
    db.conn.execute(
        """
        INSERT INTO blocks(height, hash, prev, time, nonce, size, tx_count, producer, subsidy)
        VALUES(139, ?, NULL, 0, 0, 0, 1, NULL, 0)
        """,
        ("bb" * 32,),
    )
    db.conn.execute(
        """
        INSERT INTO txs(txid, height, n, time, coinbase, identity, xid_handle, xfer_in, xfer_out, fee, vin_count, vout_count)
        VALUES(?, 139, 0, 0, 1, 0, NULL, 0, 0, 0, 1, 4)
        """,
        (txid,),
    )
    indexer._write_lottery_from_coinbase({"vout": vouts}, 139, "main")
    # leftover address-keyed row must not appear on the leaderboard
    db.conn.execute(
        """
        INSERT INTO lottery_wins(height, rank, node_id, address, amount, xaccount, is_producer)
        VALUES(34, 0, 'dead', 'XeSRy789xxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 1, NULL, 1)
        """
    )
    db.commit()
    q = Queries(db)
    leaders = q.lottery_leaders(40)
    assert [x["xaccount"] for x in leaders] == ["humble_miner"]
    assert all(not str(x["xaccount"]).startswith("X") for x in leaders)
    t = q.tx(txid)
    assert t["winner_handle"] == "humble_miner"
    assert t["lottery_handles"] == ["humble_miner", "nftrvn", "lampacenter"]
    assert t.get("xid_handle") in (None, "")
    hist = q.lottery_history(10)
    who = hist[0]["winners"][0]["xaccount"]
    assert who == "humble_miner"

    members = q.lottery_members(scope="all")
    names = [m["handle"] for m in members["items"]]
    assert names == ["humble_miner", "lampacenter", "nftrvn"]
    humble = next(m for m in members["items"] if m["handle"] == "humble_miner")
    assert humble["wins"] == 1
    assert humble["hat_blocks"] == 1
    found = q.lottery_members(q="lampa", scope="all")
    assert [m["handle"] for m in found["items"]] == ["lampacenter"]
    offline = q.lottery_members(scope="eligible", hat_handles=[], heartbeat_handles=[])
    assert offline["items"] == []
    live = q.lottery_members(scope="eligible", hat_handles=["lampacenter"])
    assert [m["handle"] for m in live["items"]] == ["lampacenter"]
    assert live["items"][0]["in_hat"] is True
    profile = q.handle_profile("humble_miner")
    assert profile["found"] is True
    assert profile["wins"] == 1
    assert profile["wins_recent"][0]["height"] == 139
    search = q.search("@lampa")
    ids = [r["id"] for r in search["results"] if r["type"] == "identity"]
    assert "lampacenter" in ids
    db.close()


def test_lottery_index_v_constant():
    assert LOTTERY_INDEX_V == "4"
    assert LOTTERY_INDEX_V != "3"
