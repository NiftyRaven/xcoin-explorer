"""Launch buy/sell classification and the public /api/trades feed."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.chain import COIN
from explorer.db import Database
from explorer.indexer import Indexer
from explorer.queries import Queries
from explorer.trades import (
    DEFAULT_LAUNCH_PROCEEDS,
    ET,
    TradeFeed,
    classify_launch_trade,
    describe_trade,
    et_day_window,
    et_midnight,
    parse_proceeds,
)

PROCEEDS = DEFAULT_LAUNCH_PROCEEDS[0]
BUYER = "XbuyerAddress111111111111111111111"
SELLER = "XsellerAddress22222222222222222222"
OTHER = "XotherAddress3333333333333333333333"
ROOT = Path(__file__).resolve().parent.parent


def leg(addr, value=0, asset=None, amount=0, kind="transfer"):
    return {
        "address": addr,
        "value": value,
        "asset": asset,
        "asset_amount": amount,
        "asset_kind": kind if asset else None,
    }


def tx(vin, vout, *, coinbase=False, height=10, txid="aa" * 32):
    return {
        "txid": txid,
        "height": height,
        "coinbase": coinbase,
        "vin": vin,
        "vout": vout,
    }


def test_parse_proceeds_list_is_configurable():
    assert DEFAULT_LAUNCH_PROCEEDS == ("XvmKQ4Rf1PtETDRMaaCeVgtKmGqqieYfQF",)
    assert parse_proceeds(" Xabc , Xdef , Xabc ") == ("Xabc", "Xdef")
    assert parse_proceeds(["Xabc", "", "Xdef"]) == ("Xabc", "Xdef")
    assert parse_proceeds("", fallback=False) == ()
    assert parse_proceeds(None) == DEFAULT_LAUNCH_PROCEEDS


def test_buy_with_change_excludes_change_and_fee():
    paid = 6000 * COIN
    change = 3999 * COIN
    fee = COIN
    received = 12345 * COIN // 10  # 1,234.5
    utxo = 2000 * COIN
    trade = classify_launch_trade(
        tx(
            [
                leg(BUYER, paid + change + fee),
                leg(PROCEEDS, 0, "MY_TOKEN", utxo),
            ],
            [
                leg(BUYER, 0, "MY_TOKEN", received),
                leg(BUYER, change),
                leg(PROCEEDS, paid),
                leg(PROCEEDS, 0, "MY_TOKEN", utxo - received),
            ],
        )
    )
    assert trade is not None
    assert trade["side"] == "buy"
    assert trade["asset"] == "MY_TOKEN"
    assert trade["asset_type"] == "root"
    assert trade["asset_atoms"] == received
    assert trade["xfer_atoms"] == paid
    assert trade["fee_atoms"] == fee
    assert trade["trader"] == BUYER
    assert trade["counterparty"] == PROCEEDS
    assert trade["price_atoms"] == paid * COIN // received
    assert trade["sentence"] == describe_trade("buy", "Xbuyer…1111", received, "MY_TOKEN", paid)
    assert "6,000 XFER" in trade["sentence"]
    assert "1,234.5 MY_TOKEN" in trade["sentence"]


def test_sell_with_change_excludes_change():
    gross = 1000 * COIN
    sold = 500 * COIN
    got = 8123 * COIN // 10  # 812.3
    launch_in = 900 * COIN
    fee = 2_000_000  # 0.02 XFER, taken out of Launch's change
    launch_change = launch_in - got - fee
    trade = classify_launch_trade(
        tx(
            [
                leg(SELLER, 0, "MY_TOKEN", gross),
                leg(PROCEEDS, launch_in),
            ],
            [
                leg(SELLER, 0, "MY_TOKEN", gross - sold),
                leg(SELLER, got),
                leg(PROCEEDS, 0, "MY_TOKEN", sold),
                leg(PROCEEDS, launch_change),
            ],
        )
    )
    assert trade is not None
    assert trade["side"] == "sell"
    assert trade["asset_atoms"] == sold
    assert trade["xfer_atoms"] == got
    assert trade["fee_atoms"] == fee
    assert trade["trader"] == SELLER
    assert "812.3 XFER" in trade["sentence"]
    assert "500 MY_TOKEN" in trade["sentence"]


def test_sell_fee_input_is_not_part_of_the_sale():
    """A fee input from the seller is not change and is not added to the sale."""
    got = 8123 * COIN // 10
    fee = COIN // 100  # 0.01 XFER
    trade = classify_launch_trade(
        tx(
            [
                leg(SELLER, 0, "MY_TOKEN", 1000 * COIN),
                leg(SELLER, fee),
                leg(PROCEEDS, got),
            ],
            [
                leg(SELLER, 0, "MY_TOKEN", 500 * COIN),
                leg(SELLER, got),
                leg(PROCEEDS, 0, "MY_TOKEN", 500 * COIN),
            ],
        )
    )
    assert trade is not None
    assert trade["side"] == "sell"
    assert trade["asset_atoms"] == 500 * COIN
    assert trade["xfer_atoms"] == got - fee
    assert trade["fee_atoms"] == fee


def test_nft_and_subasset_swaps():
    nft = classify_launch_trade(
        tx(
            [
                leg(BUYER, 50 * COIN),
                leg(PROCEEDS, 0, "ART#1", COIN),
            ],
            [
                leg(BUYER, 0, "ART#1", COIN),
                leg(PROCEEDS, 50 * COIN),
            ],
        )
    )
    assert nft is not None
    assert nft["side"] == "buy"
    assert nft["asset"] == "ART#1"
    assert nft["asset_type"] == "unique"
    assert nft["asset_atoms"] == COIN
    assert nft["xfer_atoms"] == 50 * COIN

    sub = classify_launch_trade(
        tx(
            [
                leg(SELLER, 0, "ROOT/CHILD", 10 * COIN),
                leg(PROCEEDS, 4 * COIN),
            ],
            [
                leg(PROCEEDS, 0, "ROOT/CHILD", 10 * COIN),
                leg(SELLER, 4 * COIN),
            ],
        )
    )
    assert sub is not None
    assert sub["side"] == "sell"
    assert sub["asset"] == "ROOT/CHILD"
    assert sub["asset_type"] == "sub"
    assert sub["asset_atoms"] == 10 * COIN


def test_mempool_shaped_tx_still_classifies():
    trade = classify_launch_trade(
        tx(
            [leg(BUYER, 3 * COIN), leg(PROCEEDS, 0, "MY_TOKEN", COIN)],
            [leg(BUYER, 0, "MY_TOKEN", COIN), leg(PROCEEDS, 3 * COIN)],
            height=None,
        )
    )
    assert trade is not None
    assert trade["side"] == "buy"
    assert trade["xfer_atoms"] == 3 * COIN


def test_non_trades_are_excluded():
    paid = 5 * COIN
    cases = {
        "plain xfer": tx([leg(BUYER, paid)], [leg(OTHER, paid)]),
        "xfer to proceeds only": tx([leg(BUYER, paid)], [leg(PROCEEDS, paid)]),
        "plain asset": tx(
            [leg(BUYER, 0, "MY_TOKEN", COIN)],
            [leg(OTHER, 0, "MY_TOKEN", COIN)],
        ),
        "asset to proceeds only": tx(
            [leg(SELLER, 0, "MY_TOKEN", COIN)],
            [leg(PROCEEDS, 0, "MY_TOKEN", COIN)],
        ),
        "swap away from proceeds": tx(
            [leg(BUYER, paid), leg(OTHER, 0, "MY_TOKEN", COIN)],
            [leg(BUYER, 0, "MY_TOKEN", COIN), leg(OTHER, paid)],
        ),
        "new asset": tx(
            [leg(BUYER, paid)],
            [leg(BUYER, 0, "MY_TOKEN", 1000 * COIN, "new"), leg(PROCEEDS, paid)],
        ),
        "reissue": tx(
            [leg(BUYER, paid), leg(PROCEEDS, 0, "MY_TOKEN", COIN)],
            [leg(BUYER, 0, "MY_TOKEN", 5 * COIN, "reissue"), leg(PROCEEDS, paid)],
        ),
        "coinbase": tx(
            [{"address": None, "value": 0, "asset": None, "asset_amount": 0}],
            [leg(BUYER, 5000 * COIN)],
            coinbase=True,
        ),
    }
    for name, sample in cases.items():
        assert classify_launch_trade(sample) is None, name


def test_other_proceeds_list_does_not_use_the_default():
    sample = tx(
        [leg(BUYER, 3 * COIN), leg(PROCEEDS, 0, "MY_TOKEN", COIN)],
        [leg(BUYER, 0, "MY_TOKEN", COIN), leg(PROCEEDS, 3 * COIN)],
    )
    assert classify_launch_trade(sample, proceeds=["XcustomProceeds111111111111111111"]) is None
    assert classify_launch_trade(sample, proceeds=[PROCEEDS]) is not None
    assert classify_launch_trade(sample, proceeds=[]) is None


class FakeRPC:
    def __init__(self, txs=None):
        self.connected = True
        self.txs = txs or {}
        self.methods: list[str] = []

    def try_call(self, method, *args, default=None):
        self.methods.append(method)
        if method == "getrawmempool":
            return list(self.txs)
        if method == "getrawtransaction":
            return self.txs.get(args[0], default)
        return default


def _insert_io(db, txid, n, direction, row):
    db.conn.execute(
        """
        INSERT INTO txio(
            txid, n, direction, address, value, asset, asset_amount, asset_kind, coinbase, script_type
        ) VALUES(?,?,?,?,?,?,?,?,0,'script')
        """,
        (
            txid,
            n,
            direction,
            row.get("address"),
            int(row.get("value") or 0),
            row.get("asset"),
            int(row.get("asset_amount") or 0),
            row.get("asset_kind"),
        ),
    )


def store_tx(db, sample, *, n=0, when=0, txid=None):
    txid = txid or sample["txid"]
    vin = sample["vin"]
    vout = sample["vout"]
    xfer_in = sum(int(r.get("value") or 0) for r in vin)
    xfer_out = sum(int(r.get("value") or 0) for r in vout)
    height = sample.get("height")
    db.conn.execute(
        """
        INSERT INTO txs(
            txid, height, n, time, coinbase, identity, xid_handle, xfer_in, xfer_out, fee, vin_count, vout_count
        ) VALUES(?,?,?,?,?,0,NULL,?,?,?,?,?)
        """,
        (
            txid,
            height,
            n,
            when,
            1 if sample.get("coinbase") else 0,
            xfer_in,
            xfer_out,
            max(0, xfer_in - xfer_out),
            len(vin),
            len(vout),
        ),
    )
    for i, row in enumerate(vin):
        _insert_io(db, txid, i, "in", row)
    for i, row in enumerate(vout):
        _insert_io(db, txid, i, "out", row)
    if height is not None:
        db.conn.execute(
            """
            INSERT OR REPLACE INTO blocks(height, hash, time, tx_count)
            VALUES(?,?,?,1)
            """,
            (height, f"{int(height):064x}", when),
        )


def _feed_client(db, rpc):
    indexer = Indexer(db, rpc)
    indexer.status["tip"] = 30
    app = create_app(Queries(db), indexer, rpc, launch_proceeds=[PROCEEDS])
    return TestClient(app), app


def test_feed_orders_filters_and_skips_non_trades(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    now = int(time.time())
    start, end, day = et_day_window(now)
    buy = tx(
        [leg(BUYER, 10 * COIN), leg(PROCEEDS, 0, "MY_TOKEN", 2 * COIN)],
        [leg(BUYER, 0, "MY_TOKEN", 2 * COIN), leg(PROCEEDS, 10 * COIN)],
        height=12,
        txid="b1" * 32,
    )
    sell = tx(
        [leg(SELLER, 0, "ROOT/CHILD", 4 * COIN), leg(PROCEEDS, 7 * COIN)],
        [leg(PROCEEDS, 0, "ROOT/CHILD", 4 * COIN), leg(SELLER, 7 * COIN)],
        height=11,
        txid="c2" * 32,
    )
    older = tx(
        [leg(BUYER, 1 * COIN), leg(PROCEEDS, 0, "ART#9", COIN)],
        [leg(BUYER, 0, "ART#9", COIN), leg(PROCEEDS, 1 * COIN)],
        height=9,
        txid="d3" * 32,
    )
    noise = tx(
        [leg(BUYER, 5 * COIN)],
        [leg(PROCEEDS, 5 * COIN)],
        height=10,
        txid="e4" * 32,
    )
    store_tx(db, buy, n=1, when=start)
    store_tx(db, sell, n=0, when=min(start + 90, end - 1))
    store_tx(db, older, n=0, when=start - 1)
    store_tx(db, noise, n=0, when=start)
    db.conn.execute(
        """
        INSERT INTO identities(handle, asset, address, node_id, first_height, txid)
        VALUES('bob', 'MY_TOKEN', ?, NULL, 1, 'ff')
        """,
        (BUYER,),
    )
    db.commit()

    prev_buyer = "11" * 32
    prev_market = "22" * 32
    mem_id = "33" * 32
    _insert_io(db, prev_buyer, 0, "out", leg(BUYER, 8 * COIN))
    _insert_io(db, prev_market, 0, "out", leg(PROCEEDS, 0, "MY_TOKEN", COIN))
    db.commit()
    rpc = FakeRPC(
        {
            mem_id: {
                "txid": mem_id,
                "time": now,
                "vin": [
                    {"txid": prev_buyer, "vout": 0},
                    {"txid": prev_market, "vout": 0},
                ],
                "vout": [
                    {
                        "n": 0,
                        "value": 0.0,
                        "scriptPubKey": {
                            "addresses": [BUYER],
                            "type": "transfer_asset",
                            "asset": {"name": "MY_TOKEN", "amount": 1.0},
                        },
                    },
                    {
                        "n": 1,
                        "value": 8.0,
                        "scriptPubKey": {"addresses": [PROCEEDS], "type": "pubkeyhash"},
                    },
                ],
            },
            "44" * 32: {
                "txid": "44" * 32,
                "time": now,
                "vin": [{"txid": prev_buyer, "vout": 0}],
                "vout": [
                    {
                        "n": 0,
                        "value": 8.0,
                        "scriptPubKey": {"addresses": [OTHER], "type": "pubkeyhash"},
                    }
                ],
            },
        }
    )
    client, app = _feed_client(db, rpc)
    first = client.get("/api/trades?limit=2")
    assert first.status_code == 200
    body = first.json()
    assert [item["txid"] for item in body["items"]] == [mem_id, buy["txid"], sell["txid"]]
    assert body["items"][0]["confirmed"] is False
    assert body["items"][0]["confirmations"] == 0
    assert body["items"][0]["side"] == "buy"
    assert body["items"][1]["confirmed"] is True
    assert body["items"][1]["trader_handle"] == "bob"
    assert body["items"][1]["sentence"].startswith("@bob bought")
    assert body["has_more"] is False
    assert "next_before" not in body
    shown = {item["txid"] for item in body["items"]}
    assert older["txid"] not in shown
    assert noise["txid"] not in shown

    stats = body["stats"]
    assert stats["pending"] == 1
    assert stats["trades_today"] == 3
    assert stats["volume_today_atoms"] == 8 * COIN + 10 * COIN + 7 * COIN
    assert stats["day"] == day
    assert stats["day_start"] == start
    assert "height" not in stats
    assert "trades_last_hour" not in stats
    assert "volume_24h_atoms" not in stats

    sells = client.get("/api/trades?side=sell")
    assert [item["txid"] for item in sells.json()["items"]] == [sell["txid"]]
    assert sells.json()["items"][0]["asset"] == "ROOT/CHILD"
    assert sells.json()["items"][0]["asset_type"] == "sub"

    by_asset = client.get("/api/trades?q=ART%239")
    assert by_asset.json()["items"] == []

    by_handle = client.get("/api/trades?q=%40bob")
    assert by_handle.json()["items"]
    assert all(item["trader"] == BUYER for item in by_handle.json()["items"])

    by_addr = client.get("/api/trades?q=" + SELLER)
    assert [item["txid"] for item in by_addr.json()["items"]] == [sell["txid"]]

    by_tx = client.get("/api/trades?q=" + buy["txid"])
    assert [item["txid"] for item in by_tx.json()["items"]] == [buy["txid"]]

    ignored = client.get(
        "/api/trades",
        params={"limit": 1, "before": start - 1, "before_n": 0},
    )
    ignored_body = ignored.json()
    ignored_ids = [item["txid"] for item in ignored_body["items"]]
    assert older["txid"] not in ignored_ids
    assert buy["txid"] in ignored_ids
    assert ignored_body["has_more"] is False
    assert "next_before" not in ignored_body

    again = client.get("/api/trades?limit=1")
    assert rpc.methods.count("getrawtransaction") == 2
    assert again.json()["items"][0]["txid"] == mem_id

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "css" / "app.css").read_text(encoding="utf-8")
    assert 'href="#/trades"' in html
    assert ">Trades<" in html
    assert "pageTrades" in js
    assert "Showing today's trades since 12:00 AM ET" in js
    assert "Older trades are still on the chain; open any tx, block or address to see them." in js
    assert "Confirmed means the trade is locked into the chain" in js
    assert "No Launch trades yet today." in js
    assert "Trades today" in js
    assert "XFER volume today" in js
    assert "Load older" not in js
    assert "load older" not in js.lower()
    assert "trade-in" in js
    assert "#/trades" in js
    assert ".badge.buy" in css
    assert ".badge.sell" in css
    db.close()


def _utc(y, m, d, hh, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp())


def test_et_midnight_on_standard_and_dst_days():
    """12:00 AM ET, including the 23-hour spring day and the 25-hour fall day."""
    start, key = et_midnight(_utc(2026, 1, 15, 17))
    assert (start, key) == (_utc(2026, 1, 15, 5), "2026-01-15")

    start, key = et_midnight(_utc(2026, 7, 15, 16))
    assert (start, key) == (_utc(2026, 7, 15, 4), "2026-07-15")

    spring_noon = _utc(2026, 3, 8, 16)
    start, end, key = et_day_window(spring_noon)
    assert (start, end, key) == (_utc(2026, 3, 8, 5), _utc(2026, 3, 9, 4), "2026-03-08")
    assert end - start == 23 * 3600
    prev, prev_key = et_midnight(start - 1)
    assert (prev, prev_key) == (_utc(2026, 3, 7, 5), "2026-03-07")

    fall_noon = _utc(2026, 11, 1, 17)
    start, end, key = et_day_window(fall_noon)
    assert (start, end, key) == (_utc(2026, 11, 1, 4), _utc(2026, 11, 2, 5), "2026-11-01")
    assert end - start == 25 * 3600
    prev, prev_key = et_midnight(start - 1)
    assert (prev, prev_key) == (_utc(2026, 10, 31, 4), "2026-10-31")

    first = datetime(2026, 11, 1, 1, 30, tzinfo=ET, fold=0)
    second = datetime(2026, 11, 1, 1, 30, tzinfo=ET, fold=1)
    assert int(first.timestamp()) != int(second.timestamp())
    assert et_midnight(first.timestamp()) == et_midnight(second.timestamp())
    assert et_midnight(first.timestamp()) == (_utc(2026, 11, 1, 4), "2026-11-01")
    window_start, window_end, _ = et_day_window(first.timestamp())
    assert window_start <= int(first.timestamp()) < window_end
    assert window_start <= int(second.timestamp()) < window_end


def _buy(txid, height, asset="MY_TOKEN", paid=10 * COIN):
    return tx(
        [leg(BUYER, paid), leg(PROCEEDS, 0, asset, 2 * COIN)],
        [leg(BUYER, 0, asset, 2 * COIN), leg(PROCEEDS, paid)],
        height=height,
        txid=txid,
    )


def _table_names(db) -> set[str]:
    rows = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row["name"] for row in rows}


def _assert_day_rollover(db_path: Path, start: int, end: int, day_key: str, next_key: str):
    """Trades before 12:00 AM ET stay out, and the in-memory day drops at the next midnight."""
    db = Database(db_path)
    yesterday = _buy("a1" * 32, 1, "YESTERDAY")
    today = _buy("b2" * 32, 2, "TODAY")
    late = _buy("c3" * 32, 4, "LATE")
    tomorrow = _buy("d4" * 32, 3, "TOMORROW")
    store_tx(db, yesterday, when=start - 1)
    store_tx(db, today, n=1, when=start)
    store_tx(db, late, when=end - 1)
    store_tx(db, tomorrow, when=end)
    prev_buyer = "11" * 32
    prev_market = "22" * 32
    _insert_io(db, prev_buyer, 0, "out", leg(BUYER, 8 * COIN))
    _insert_io(db, prev_market, 0, "out", leg(PROCEEDS, 0, "MY_TOKEN", COIN))
    db.commit()
    tables = _table_names(db)

    def mem_tx(txid, when):
        return {
            "txid": txid,
            "time": when,
            "vin": [
                {"txid": prev_buyer, "vout": 0},
                {"txid": prev_market, "vout": 0},
            ],
            "vout": [
                {
                    "n": 0,
                    "value": 0.0,
                    "scriptPubKey": {
                        "addresses": [BUYER],
                        "type": "transfer_asset",
                        "asset": {"name": "MY_TOKEN", "amount": 1.0},
                    },
                },
                {
                    "n": 1,
                    "value": 8.0,
                    "scriptPubKey": {"addresses": [PROCEEDS], "type": "pubkeyhash"},
                },
            ],
        }

    old_mem = "e5" * 32
    today_mem = "f6" * 32
    rpc = FakeRPC(
        {
            old_mem: mem_tx(old_mem, start - 5),
            today_mem: mem_tx(today_mem, start + 10),
        }
    )
    feed = TradeFeed(db, rpc, None, proceeds=[PROCEEDS])

    noon = feed.page(now=start + 12 * 3600)
    noon_ids = {item["txid"] for item in noon["items"]}
    assert noon["day"] == day_key
    assert noon["day_start"] == start
    assert noon["day_end"] == end
    assert noon["has_more"] is False
    assert today["txid"] in noon_ids
    assert late["txid"] in noon_ids
    assert today_mem in noon_ids
    assert yesterday["txid"] not in noon_ids
    assert tomorrow["txid"] not in noon_ids
    assert old_mem not in noon_ids
    assert noon["stats"]["pending"] == 1
    assert noon["stats"]["trades_today"] == 3
    assert noon["stats"]["volume_today_atoms"] == 10 * COIN + 10 * COIN + 8 * COIN
    assert {row["txid"] for row in feed._day_trades} == {today["txid"], late["txid"]}

    at_open = feed.page(now=start)
    assert today["txid"] in {item["txid"] for item in at_open["items"]}
    assert yesterday["txid"] not in {item["txid"] for item in at_open["items"]}
    assert at_open["day"] == day_key

    still_today = feed.page(now=end - 1)
    still_ids = {item["txid"] for item in still_today["items"]}
    assert still_today["day"] == day_key
    assert late["txid"] in still_ids
    assert tomorrow["txid"] not in still_ids

    rolled = feed.page(now=end)
    rolled_ids = {item["txid"] for item in rolled["items"]}
    assert rolled["day"] == next_key
    assert rolled["day_start"] == end
    assert rolled["has_more"] is False
    assert rolled_ids == {tomorrow["txid"]}
    assert rolled["stats"]["trades_today"] == 1
    assert rolled["stats"]["pending"] == 0
    assert rolled["stats"]["volume_today_atoms"] == 10 * COIN
    assert feed._day_key == next_key
    assert {row["txid"] for row in feed._day_trades} == {tomorrow["txid"]}
    assert _table_names(db) == tables
    assert "trade_history" not in tables
    assert "launch_trades" not in tables
    db.close()


def test_feed_drops_previous_et_day_at_midnight_including_dst(tmp_path: Path):
    _assert_day_rollover(
        tmp_path / "spring.db",
        _utc(2026, 3, 8, 5),
        _utc(2026, 3, 9, 4),
        "2026-03-08",
        "2026-03-09",
    )
    _assert_day_rollover(
        tmp_path / "fall.db",
        _utc(2026, 11, 1, 4),
        _utc(2026, 11, 2, 5),
        "2026-11-01",
        "2026-11-02",
    )
