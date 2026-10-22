"""All-time Launch trades from the relay's public markets + auctions."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.db import Database
from explorer.launch_trades import (
    RelayTrades,
    human_atoms,
    index_launch_fills,
    trades_from_auctions,
    trades_from_markets,
)
from explorer.queries import Queries

DATA = json.loads((Path(__file__).parent / "data" / "launch_markets.json").read_text())
BOOK = "XvmKQ4Rf1PtETDRMaaCeVgtKmGqqieYfQF"
RAVEN = "Xi3UpFEU3nx9uqKB2sRxCUE9ur7PMoexoy"
AUCTION = {"auctions": [{"id": "a1", "name": "EVILRA99#XFERSTICKERS_9", "qtyRaw": "1", "units": 0, "seller": "XsellerZ",
                          "settleTxid": "ab" * 32, "winner": "XwinnerZ", "soldXferons": "700000000", "endAt": 1791600000000},
                         {"id": "a2", "name": "OPEN#ONE", "settleTxid": None, "winner": None}]}


class Rpc:
    connected = False


def fake_fetch(url):
    if url.endswith("/api/launch/markets"):
        return DATA
    if url.endswith("/api/auctions"):
        return AUCTION
    raise AssertionError(url)


def client(tmp_path, fetch=fake_fetch):
    db = Database(tmp_path / "x.sqlite")
    db.conn.execute("INSERT INTO identities(handle, address) VALUES ('ravennifty', ?)", (RAVEN,))
    # Book fill a2749a2e is booked to the escrow; the chain inputs name the buyer.
    db.conn.execute(
        "INSERT INTO txio(txid, n, direction, address, value) VALUES (?, 0, 'in', ?, 600000000000)",
        ("a2749a2e1aa623cce34551ef6b262f08bffcd9304fa4529e7b5e3b8b8c78eccc", RAVEN),
    )
    db.conn.commit()
    app = create_app(Queries(db), None, Rpc(), relay_trades=RelayTrades("http://relay", fetch=fetch))
    return TestClient(app)


def test_markets_cover_every_user_and_asset():
    rows = trades_from_markets(DATA["markets"])
    assert len(rows) == 33
    assert len({r["trader"] for r in rows}) > 5
    ids = {r["txid"][:8]: r for r in rows}
    assert ids["c3acda69"]["asset"] == "EVILRA99#XFERSTICKERS_1"
    assert ids["c3acda69"]["xfer_atoms"] == 50_000 * 10**8
    assert ids["c3acda69"]["asset_atoms"] == 10**8
    assert sum(r["asset"] == "RIZIGIDY/TICKERBOX_20261009" for r in rows) == 17
    assert ids["a2749a2e"]["venue"] == "book"


def test_auction_settlements_only():
    rows = trades_from_auctions(AUCTION["auctions"])
    assert [r["venue"] for r in rows] == ["auction"]
    assert rows[0]["trader"] == "XwinnerZ"


def test_api_all_time_paged_filtered(tmp_path):
    c = client(tmp_path)
    body = c.get("/api/trades?limit=25").json()
    assert body["total"] == 34 and len(body["items"]) == 25 and body["has_more"] is True
    assert body["stats"]["trades_all_time"] == 34
    assert isinstance(body["stats"].get("trades_today"), int)
    assert isinstance(body["stats"].get("window_start"), int)
    assert body["stats"]["trades_today"] <= body["stats"]["trades_all_time"]
    page2 = c.get("/api/trades?limit=25&offset=25").json()
    assert len(page2["items"]) == 9 and page2["has_more"] is False
    seen = {t["txid"] for t in body["items"] + page2["items"]}
    assert len(seen) == 34
    times = [t["time"] for t in body["items"] + page2["items"]]
    assert times == sorted(times, reverse=True)
    assert "proceeds" not in body

    book = c.get("/api/trades?q=a2749a2e1aa623cce34551ef6b262f08bffcd9304fa4529e7b5e3b8b8c78eccc").json()["items"][0]
    assert book["trader"] == RAVEN and book["trader_handle"] == "ravennifty"
    assert book["sentence"].startswith("@ravennifty bought")

    stickers = c.get("/api/trades?q=XFERSTICKERS_12").json()["items"]
    assert [t["xfer_atoms"] for t in stickers] == [50_000 * 10**8]
    sells = c.get("/api/trades?side=sell&limit=100").json()["items"]
    assert sells and all(t["side"] == "sell" for t in sells)
    mine = c.get("/api/trades?q=@ravennifty&limit=100").json()["items"]
    assert mine and all(t["trader"] == RAVEN for t in mine)
    other = c.get("/api/trades?q=XrUihCjaK56myBohRdueoKYZibdPSWxqFf").json()["items"]
    assert len(other) == 1


def test_relay_down_is_not_fatal(tmp_path):
    def boom(url):
        raise OSError("down")

    body = client(tmp_path, boom).get("/api/trades").json()
    assert body["items"] == [] and body["total"] == 0


PAY = "7bd001863679ed0f37bd8c892a0487928148228e014ca87e66e818159f2d8000"
DELIVERY = "2deeb47b4c70d97cc84902956a2d4891723b0551e8b6d41755b4971ab438a7f4"
PENDING = "ab" * 32
SELL = "cd" * 32


def _dogx_markets():
    return {
        "markets": [
            {
                "name": "LAUNCH_XFER/DOGX",
                "units": 4,
                "fills": [
                    {
                        "txid": PAY,
                        "side": "buy",
                        "address": "Xesi6njEw3doByxiaDGH1h5G2W9soKKM7L",
                        "xferons": "5423350448",
                        "tokens": "5155236518",
                        "feeXferons": "32540102",
                        "creatorFeeXferons": "16417811",
                        "deliveryTxid": DELIVERY,
                        "time": 1792692398931,
                    },
                    {
                        "txid": PENDING,
                        "side": "buy",
                        "xferons": "100000000",
                        "tokens": "10000",
                        "feeXferons": "600000",
                        "creatorFeeXferons": "300000",
                        "time": 1,
                    },
                    {
                        "txid": SELL,
                        "side": "sell",
                        "xferons": "2649841",
                        "tokens": "1000",
                        "feeXferons": "15899",
                        "creatorFeeXferons": "7949",
                        "time": 2,
                    },
                    {
                        "txid": "ee" * 32,
                        "side": "buy",
                        "source": "refund",
                        "xferons": "198999998",
                        "tokens": "0",
                        "feeXferons": "0",
                        "refundTxid": "ff" * 32,
                        "time": 3,
                    },
                ],
            }
        ]
    }


def test_human_amounts_match_the_dogx_fill():
    assert human_atoms(51_552_365_180_000) == "515,523.65"
    assert human_atoms(5_423_350_448) == "54.23"
    assert human_atoms(32_540_102) == "0.32540102"
    assert human_atoms(1_000_000) == "0.01"
    assert human_atoms(10_520) == "0.0001052"


def test_fill_index_links_payment_and_delivery():
    idx = index_launch_fills(_dogx_markets()["markets"])
    pay = idx[PAY]
    assert pay["role"] == "payment"
    assert pay["headline"] == "Launch trade: Bought 515,523.65 DOGX for 54.23 XFER"
    assert pay["symbol"] == "DOGX"
    assert pay["asset"] == "LAUNCH_XFER/DOGX"
    assert pay["amount"] == "515,523.65 DOGX"
    assert pay["price_atoms"] == 10_520
    assert pay["market_fee_atoms"] == 32_540_102
    assert pay["creator_fee_atoms"] == 16_417_811
    assert pay["delivery_txid"] == DELIVERY
    assert pay["delivering"] is False
    delivery = idx[DELIVERY]
    assert delivery["role"] == "delivery"
    assert delivery["headline"] == "Asset transfer for Launch trade"
    assert delivery["payment_txid"] == PAY
    assert idx[PENDING]["delivering"] is True
    assert idx[PENDING]["delivery_txid"] is None
    assert idx[SELL]["headline"].startswith("Launch trade: Sold ")
    assert idx[SELL]["delivering"] is False
    assert "ee" * 32 not in idx
    assert "ff" * 32 not in idx


def test_fixture_indexes_every_payment_and_delivery():
    idx = index_launch_fills(DATA["markets"])
    saw_delivery = False
    for market in DATA["markets"]:
        for fill in market.get("fills") or []:
            if fill.get("source") == "refund":
                assert fill["txid"].lower() not in idx
                continue
            pay = fill["txid"].lower()
            assert idx[pay]["role"] == "payment"
            delivery = str(fill.get("deliveryTxid") or "").lower()
            if not delivery:
                continue
            saw_delivery = True
            assert idx[delivery]["role"] == "delivery"
            assert idx[delivery]["payment_txid"] == pay
    assert saw_delivery


def test_tx_page_launch_trade_panel(tmp_path):
    def fetch(url):
        if url.endswith("/api/launch/markets"):
            return _dogx_markets()
        if url.endswith("/api/auctions"):
            return {"auctions": []}
        raise AssertionError(url)

    db = Database(tmp_path / "x.sqlite")
    db.conn.execute(
        "INSERT INTO txs(txid, n, time, fee, coinbase) VALUES (?, 0, 1700000000, ?, 0)",
        (PAY, 1_000_000),
    )
    db.conn.execute(
        "INSERT INTO txs(txid, n, time, fee, coinbase) VALUES (?, 0, 1700000000, ?, 0)",
        (DELIVERY, 503_270),
    )
    db.conn.execute(
        "INSERT INTO txs(txid, n, time, fee, coinbase) VALUES (?, 0, 1700000000, 0, 0)",
        ("11" * 32,),
    )
    db.conn.commit()
    app = create_app(Queries(db), None, Rpc(), relay_trades=RelayTrades("http://relay", fetch=fetch))
    c = TestClient(app)
    pay = c.get(f"/api/tx/{PAY}").json()["launch_trade"]
    assert pay["headline"] == "Launch trade: Bought 515,523.65 DOGX for 54.23 XFER"
    assert pay["network_fee"] == "0.01 XFER"
    assert pay["network_fee_atoms"] == 1_000_000
    assert pay["market_fee"] == "0.32540102 XFER"
    assert pay["creator_fee"] == "0.16417811 XFER"
    assert pay["delivery_txid"] == DELIVERY
    delivery = c.get(f"/api/tx/{DELIVERY}").json()["launch_trade"]
    assert delivery["headline"] == "Asset transfer for Launch trade"
    assert delivery["payment_txid"] == PAY
    assert delivery["role"] == "delivery"
    assert "network_fee" not in delivery
    plain = c.get(f"/api/tx/{'11' * 32}").json()
    assert "launch_trade" not in plain

    def boom(url):
        raise OSError("down")

    down = create_app(Queries(db), None, Rpc(), relay_trades=RelayTrades("http://relay", fetch=boom))
    body = TestClient(down).get(f"/api/tx/{PAY}").json()
    assert body["txid"] == PAY
    assert "launch_trade" not in body


def test_launch_trade_copy_avoids_banned_words():
    root = Path(__file__).resolve().parent.parent
    js = (root / "web" / "js" / "app.js").read_text(encoding="utf-8")
    panel = js[js.index("function launchTradePanel") : js.index("async function pageTx")]
    py = (root / "explorer" / "launch_trades.py").read_text(encoding="utf-8")
    for blob in (panel, py):
        low = blob.lower()
        for word in ("earn", "yield", "apy", "profit", "guaranteed"):
            assert word not in low
    assert "Delivering…" in panel
    assert "Asset transfer for Launch trade" in panel
    assert "infoTip(" in panel
    assert "function infoTip" in js
