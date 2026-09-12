from explorer.amounts import format_xfer, xfer_to_atoms
from explorer.chain import (
    COIN,
    INITIAL_SUBSIDY,
    circulating_supply,
    classify_asset_name,
    slot_from_height,
    subsidy_at,
    winner_count,
)
from explorer.decode import classify_search, parse_xhb1, parse_xid1, parse_xva1, select_winners, split_reward


def test_subsidy_schedule():
    assert subsidy_at(0) == 0
    assert subsidy_at(1) == INITIAL_SUBSIDY
    assert subsidy_at(2_099_999) == INITIAL_SUBSIDY
    assert subsidy_at(2_100_000) == INITIAL_SUBSIDY // 2
    assert winner_count(1) == 1
    assert winner_count(2_100_000) == 2
    assert winner_count(4_200_000) == 3


def test_circulating_supply():
    assert circulating_supply(0) == 0
    assert circulating_supply(1) == INITIAL_SUBSIDY
    assert circulating_supply(10) == 10 * INITIAL_SUBSIDY
    # first era is heights 1..2_099_999
    first_era = 2_099_999 * INITIAL_SUBSIDY
    assert circulating_supply(2_099_999) == first_era
    assert circulating_supply(2_100_000) == first_era + INITIAL_SUBSIDY // 2


def test_slot_mapping():
    genesis = 1_789_197_360
    assert slot_from_height(0, genesis) == genesis // 60
    assert slot_from_height(1, genesis) == genesis // 60 + 1


def test_format_xfer():
    assert format_xfer(0) == "0 XFER"
    assert format_xfer(COIN) == "1 XFER"
    assert format_xfer(5_000 * COIN) == "5,000 XFER"
    assert xfer_to_atoms(1.5) == int(1.5 * COIN)


def test_asset_kinds():
    assert classify_asset_name("NFTRVN") == "root"
    assert classify_asset_name("NFTRVN!") == "owner"
    assert classify_asset_name("NFTRVN/CHILD") == "sub"
    assert classify_asset_name("NFTRVN#tag") == "unique"


def test_xhb1_and_xid1():
    # OP_RETURN PUSH XHB1 + count=1 + 20 zero bytes
    payload = b"XHB1" + (1).to_bytes(4, "little") + (b"\x01" * 20)
    script = bytes([0x6A, len(payload)]) + payload
    parsed = parse_xhb1(script)
    assert parsed is not None
    assert len(parsed.active_ids) == 1
    assert parsed.active_ids[0] == ("01" * 20)  # reversed of 20 0x01 is still 01s

    xid_payload = b"XID1" + b"nftrvn"
    xid_script = bytes([0x6A, len(xid_payload)]) + xid_payload
    assert parse_xid1(xid_script) == "nftrvn"


def test_xva1_handle_id_stamp():
    from explorer.decode import rpc_hex

    handle = b"nftrvn"
    node_id = b"\x02" * 20
    stamp = b"\x03" * 65
    body = bytes([len(handle)]) + handle + node_id + stamp
    payload = b"XVA1" + (1).to_bytes(4, "little") + body
    script = bytes([0x6A, 0x4C, len(payload)]) + payload
    rows = parse_xva1(script)
    assert rows is not None
    assert len(rows) == 1
    assert rows[0].handle == "nftrvn"
    assert rows[0].node_id == rpc_hex(node_id)
    assert rows[0].stamp == stamp.hex()


def test_select_winners_and_split():
    ids = ["aa" * 20, "bb" * 20, "cc" * 20]
    seed = "11" * 32
    w = select_winners(ids, seed, 1)
    assert len(w) == 1
    assert w[0] in ids
    assert select_winners(ids, seed, 10) == ids
    assert split_reward(100, 3) == [34, 33, 33]


def test_classify_search():
    assert classify_search("12") == "height"
    assert classify_search("@" + "nftrvn") == "handle"
    assert classify_search("NFTRVN/CHILD") == "asset"
    assert classify_search("ab" * 32) == "hash"
