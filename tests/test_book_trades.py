"""Order book trades (XB1 memo) on the public Trades page."""

from __future__ import annotations

import time
from pathlib import Path

from explorer.chain import COIN
from explorer.db import Database
from explorer.trades import (
    LAUNCH_TREASURY,
    TradeFeed,
    classify_launch_trade,
    decode_route_memo,
    et_day_window,
)

ASSET = "NFTRVN/DEMO"
UNITS = {ASSET: 2}
TAKER = "XtakerMain11111111111111111111111"
TAKER_ALT = "XtakerAlt222222222222222222222222"
MAKER = "XmakerAddr33333333333333333333333"
ESCROW_T = "XescrowTok44444444444444444444444"
ESCROW_X = "XescrowXfr55555555555555555555555"
FEE = "XlaunchFee66666666666666666666666"
CREATOR = "XcreatorVt77777777777777777777777"
RESERVE = "XreserveDm88888888888888888888888"
ATTACKER = "Xattacker99999999999999999999999"


def leg(addr, value=0, asset=None, amount=0):
    return {"address": addr, "value": value, "asset": asset, "asset_amount": amount, "asset_kind": "transfer" if asset else None}


def memo(text, hexed=False):
    return {"address": None, "value": 0, "asset": None, "asset_amount": 0, "asset_kind": None, "op_return": text.encode().hex() if hexed else text}


def tx(vin, vout, *, height=10, txid="aa" * 32):
    return {"txid": txid, "height": height, "coinbase": False, "vin": vin, "vout": vout}


def book_buy(fee_to=FEE, *, hexed=False, txid="b0" * 32, height=10):
    """Taker buys 1,500 tokens (150000 raw at 2 decimals) from one resting sell at 2 XFER."""
    qty_raw = 150_000
    atoms = qty_raw * 10**6
    notional = 3000 * COIN
    platform = notional * 60 // 10_000
    creator = notional * 30 // 10_000
    net_fee = COIN // 100
    funds = 4000 * COIN
    change = funds - notional - platform - creator - net_fee
    return tx(
        [leg(TAKER, funds), leg(ESCROW_T, 0, ASSET, atoms)],
        [
            leg(TAKER, 0, ASSET, atoms),
            leg(MAKER, notional),
            leg(fee_to, platform),
            leg(CREATOR, creator),
            leg(TAKER_ALT, change),
            memo(f"XB1|B|{ASSET}|{qty_raw}|{notional}", hexed),
        ],
        txid=txid,
        height=height,
    )


def test_decode_route_memo():
    assert decode_route_memo("XB1|B|NFTRVN/DEMO|150000|300000000000") == {
        "side": "buy",
        "asset": "NFTRVN/DEMO",
        "tokens": 150000,
        "value_atoms": 300000000000,
    }
    assert decode_route_memo("XB1|X|A|1|1") is None
    assert decode_route_memo("XB1|B|A|0|1") is None
    assert decode_route_memo("XL1|B|A|1|1") is None


def test_book_buy_counts_when_the_launch_fee_address_is_paid():
    trade = classify_launch_trade(book_buy(), asset_units=UNITS, fee_addresses={FEE})
    assert trade is not None
    assert trade["venue"] == "book"
    assert trade["side"] == "buy"
    assert trade["trader"] == TAKER
    assert trade["asset"] == ASSET
    assert trade["asset_atoms"] == 150_000 * 10**6
    assert trade["xfer_atoms"] == 3000 * COIN
    assert trade["price_atoms"] == 2 * COIN
    assert trade["tokens_pending"] is False
    # Hex memos (how the index stores them) read the same.
    assert classify_launch_trade(book_buy(hexed=True), asset_units=UNITS, fee_addresses={FEE})["side"] == "buy"


def test_book_trade_without_a_launch_fee_is_not_a_trade():
    # Fee address unknown: not proven.
    assert classify_launch_trade(book_buy(), asset_units=UNITS) is None
    # Copied memo paying the "fee" to someone else.
    assert classify_launch_trade(book_buy(fee_to=ATTACKER), asset_units=UNITS, fee_addresses={FEE}) is None


def test_book_fee_paid_to_the_treasury_counts():
    trade = classify_launch_trade(book_buy(fee_to=LAUNCH_TREASURY), asset_units=UNITS)
    assert trade and trade["venue"] == "book"


def test_mixed_route_buy_book_then_curve():
    """Book fill at 2 XFER plus a curve part: curve net to the reserve, fee to the treasury."""
    qty_book = 100_000
    qty_curve = 50_000
    book_notional = 2000 * COIN
    curve_gross = 1050 * COIN
    curve_net = curve_gross * 9910 // 10_000
    curve_plat = curve_gross * 60 // 10_000
    curve_creator = curve_gross - curve_net - curve_plat
    book_plat = book_notional * 60 // 10_000
    book_creator = book_notional * 30 // 10_000
    funds = 5000 * COIN
    spent = book_notional + book_plat + book_creator + curve_gross + COIN // 100
    value = book_notional + curve_gross
    t = tx(
        [leg(TAKER, funds), leg(ESCROW_T, 0, ASSET, qty_book * 10**6)],
        [
            leg(TAKER, 0, ASSET, qty_book * 10**6),
            leg(MAKER, book_notional),
            leg(RESERVE, curve_net),
            leg(LAUNCH_TREASURY, curve_plat),
            leg(CREATOR, curve_creator + book_creator),
            leg(FEE, book_plat),
            leg(TAKER_ALT, funds - spent),
            memo(f"XB1|B|{ASSET}|{qty_book + qty_curve}|{value}"),
        ],
    )
    # Treasury fee alone covers only the curve part.
    assert classify_launch_trade(t, asset_units=UNITS) is None
    trade = classify_launch_trade(t, asset_units=UNITS, reserves={ASSET: RESERVE})
    assert trade and trade["asset_atoms"] == (qty_book + qty_curve) * 10**6
    assert trade["xfer_atoms"] == value
    assert classify_launch_trade(t, asset_units=UNITS, fee_addresses={FEE})["trader"] == TAKER


def test_book_sell_names_the_seller():
    """Taker sells into a resting buy. The maker's deposit pays the notional and fees."""
    qty_raw = 40_000
    atoms = qty_raw * 10**6
    notional = 800 * COIN
    platform = notional * 60 // 10_000
    creator = notional * 30 // 10_000
    deposit = notional + platform + creator
    t = tx(
        [leg(TAKER_ALT, COIN), leg(ESCROW_X, deposit), leg(TAKER, 0, ASSET, atoms)],
        [
            leg(MAKER, 0, ASSET, atoms),
            leg(TAKER, notional),
            leg(FEE, platform),
            leg(CREATOR, creator),
            leg(TAKER_ALT, COIN - COIN // 100),
            memo(f"XB1|S|{ASSET}|{qty_raw}|{notional}"),
        ],
    )
    trade = classify_launch_trade(t, asset_units=UNITS, fee_addresses={FEE})
    assert trade["side"] == "sell"
    assert trade["trader"] == TAKER
    assert trade["asset_atoms"] == atoms


def _store(db, sample, *, n, when):
    vin, vout = sample["vin"], sample["vout"]
    xin = sum(int(r.get("value") or 0) for r in vin)
    xout = sum(int(r.get("value") or 0) for r in vout)
    db.conn.execute(
        """INSERT INTO txs(txid, height, n, time, coinbase, identity, xid_handle, xfer_in, xfer_out, fee, vin_count, vout_count)
        VALUES(?,?,?,?,0,0,NULL,?,?,?,?,?)""",
        (sample["txid"], sample["height"], n, when, xin, xout, max(0, xin - xout), len(vin), len(vout)),
    )
    for direction, rows in (("in", vin), ("out", vout)):
        for i, row in enumerate(rows):
            db.conn.execute(
                """INSERT INTO txio(txid, n, direction, address, value, asset, asset_amount, asset_kind, coinbase, script_type, op_return)
                VALUES(?,?,?,?,?,?,?,?,0,?,?)""",
                (sample["txid"], i, direction, row.get("address"), int(row.get("value") or 0), row.get("asset"),
                 int(row.get("asset_amount") or 0), row.get("asset_kind"), "nulldata" if row.get("op_return") else "script", row.get("op_return")),
            )
    db.conn.execute("INSERT OR REPLACE INTO blocks(height, hash, time, tx_count) VALUES(?,?,?,1)", (sample["height"], f"{sample['height']:064x}", when))


def _fair_sell(txid, height):
    """Verified XL1 sell paid by the listing reserve. The 0.60% goes to the Launch fee address."""
    tokens = 20_000
    atoms = tokens * 10**6
    payout = 400 * COIN
    plat = payout * 60 // 10_000
    creator = payout * 30 // 10_000
    reserve_in = 1000 * COIN
    return tx(
        [leg(MAKER, 0, ASSET, atoms), leg(MAKER, COIN), leg(RESERVE, reserve_in)],
        [
            leg(LAUNCH_TREASURY, 0, ASSET, atoms),
            leg(MAKER, payout - plat - creator + COIN - COIN // 100),
            leg(RESERVE, reserve_in - payout),
            leg(FEE, plat),
            leg(CREATOR, creator),
            memo(f"XL1|S|{ASSET}|{payout}|{tokens}", hexed=True),
        ],
        txid=txid,
        height=height,
    )


def _db(tmp_path, name):
    db = Database(tmp_path / name)
    db.conn.execute("INSERT INTO assets(name, kind, units) VALUES(?, 'sub', 2)", (ASSET,))
    db.conn.execute("INSERT INTO launch_reserves(asset, address, txid) VALUES(?,?,?)", (ASSET, RESERVE, "c0" * 32))
    return db


def test_feed_shows_book_fills_after_a_sell_teaches_the_fee_address(tmp_path: Path):
    db = _db(tmp_path, "book.db")
    start, _end, _day = et_day_window(int(time.time()))
    sell = _fair_sell("e1" * 32, 8)
    take = book_buy(hexed=True, txid="b1" * 32, height=9)
    _store(db, sell, n=1, when=start + 10)
    _store(db, take, n=1, when=start + 20)
    db.commit()
    page = TradeFeed(db, None, None, cache_seconds=0).page(now=start + 60)
    shown = {item["txid"]: item for item in page["items"]}
    assert shown[sell["txid"]]["side"] == "sell"
    assert shown[take["txid"]]["venue"] == "book"
    assert shown[take["txid"]]["trader"] == TAKER
    assert shown[take["txid"]]["confirmed"] is True
    assert page["stats"]["trades_today"] == 2
    saved = db.conn.execute("SELECT address FROM launch_fee_addresses").fetchall()
    assert [r["address"] for r in saved] == [FEE]
    db.close()


def test_feed_learns_the_fee_address_from_an_older_sell(tmp_path: Path):
    db = _db(tmp_path, "older.db")
    start, _end, _day = et_day_window(int(time.time()))
    sell = _fair_sell("e2" * 32, 5)
    take = book_buy(hexed=True, txid="b2" * 32, height=9)
    _store(db, sell, n=1, when=start - 3600)
    _store(db, take, n=1, when=start + 20)
    db.commit()
    page = TradeFeed(db, None, None, cache_seconds=0).page(now=start + 60)
    ids = [item["txid"] for item in page["items"]]
    assert ids == [take["txid"]]


def test_forged_book_memo_stays_off_the_page(tmp_path: Path):
    db = _db(tmp_path, "forged.db")
    start, _end, _day = et_day_window(int(time.time()))
    _store(db, _fair_sell("e3" * 32, 5), n=1, when=start - 3600)
    fake = book_buy(fee_to=ATTACKER, hexed=True, txid="b3" * 32, height=9)
    _store(db, fake, n=1, when=start + 20)
    db.commit()
    page = TradeFeed(db, None, None, cache_seconds=0).page(now=start + 60)
    assert page["items"] == []
