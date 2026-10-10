"""All-time Launch trades from the relay's public markets + auctions."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.db import Database
from explorer.launch_trades import RelayTrades, trades_from_auctions, trades_from_markets
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
