"""Address -> X handle links: only provable ones, never a plain recipient."""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.config import Settings
from explorer.db import Database
from explorer.indexer import Indexer
from explorer.links import AddressLinks, addresses_in, sighash_flags
from explorer.queries import Queries
from explorer.rpc import XCoinRPC

TREASURY = "XmLv1ZYu8qMsGTsWvD9N7C7AFcK844nHwF"


def _tx(db, txid, height, ins=(), outs=(), sighash_all=True):
    db.conn.execute("INSERT INTO txs(txid, height, n) VALUES(?,?,0)", (txid, height))
    for n, (addr, asset, spent) in enumerate(ins):
        db.conn.execute(
            "INSERT INTO txio(txid, n, direction, address, asset, asset_amount, spent_txid, spent_n) VALUES(?,?,?,?,?,?,?,?)",
            (txid, n, "in", addr, asset, 1 if asset else 0, spent[0] if spent else None, spent[1] if spent else None),
        )
    for n, (addr, asset, kind, value, memo) in enumerate(outs):
        db.conn.execute(
            "INSERT INTO txio(txid, n, direction, address, value, asset, asset_amount, asset_kind, op_return) VALUES(?,?,?,?,?,?,?,?,?)",
            (txid, n, "out", addr, value, asset, 1 if asset else 0, kind, memo),
        )
    db.conn.execute("INSERT OR REPLACE INTO tx_sighash(txid, all_sighash_all) VALUES(?,?)", (txid, 1 if sighash_all else 0))


def _out(addr, asset=None, kind=None, value=1000, memo=None):
    return (addr, asset, kind, value, memo)


def _chain(tmp_path: Path, relay=None):
    db = Database(tmp_path / "l.db")
    links = AddressLinks(db, None, (TREASURY,), "http://relay" if relay else "", fetch_json=relay)
    db.conn.execute(
        "INSERT INTO identities(handle, asset, address, txid) VALUES('evil', 'EVIL', 'XRoot', 'claim')"
    )
    db.conn.execute(
        "INSERT INTO identities(handle, asset, address, txid) VALUES('launch_xfer', 'LAUNCH_XFER', ?, 'lx')", (TREASURY,)
    )
    # Claim: EVIL + EVIL! created.
    _tx(db, "claim", 1, ins=[("XPayRoot", None, ("f0", 0))], outs=[_out("XRoot", "EVIL", "new", 0), _out("XOwn1", "EVIL!", "new", 0)])
    # Sub-asset issue: spends EVIL! from XOwn1 with XFER from XFee; owner change to XOwn2; new asset to XSub.
    _tx(
        db,
        "issue",
        2,
        ins=[("XFee", None, ("f1", 0)), ("XOwn1", "EVIL!", ("claim", 1))],
        outs=[_out("XBurn"), _out("XRoot"), _out("XOwn2", "EVIL!", "transfer", 0), _out("XSub", "EVIL/STICK", "new", 0)],
    )
    # XSub lists on Launch: sends the asset to the treasury, fee paid by XFee (same wallet).
    _tx(db, "list", 3, ins=[("XFee", None, ("f2", 0)), ("XSub", "EVIL/STICK", ("issue", 3))], outs=[_out(TREASURY, "EVIL/STICK", "transfer", 0)])
    # A buyer receives the asset from the treasury: never the seller's wallet.
    _tx(db, "deliver", 4, ins=[(TREASURY, "EVIL/STICK", ("list", 0)), ("XBuyerPay", None, ("f3", 0))], outs=[_out("XBuyer", "EVIL/STICK", "transfer", 0)])
    # A Launch buy memo with two of someone's inputs: skipped (Launch tx).
    _tx(db, "buy", 5, ins=[("XSub", None, ("f4", 0)), ("XStranger", None, ("f5", 0))], outs=[_out(TREASURY, memo="584c317c427c41")])
    # A partial swap (SIGHASH_SINGLE|ANYONECANPAY on one side): skipped.
    _tx(db, "swap", 6, ins=[("XFee", None, ("f6", 0)), ("XOther", None, ("f7", 0))], outs=[_out("XOther")], sighash_all=False)
    # EVIL! given away in a plain transfer to someone else: the path ends there.
    _tx(db, "gift", 7, ins=[("XOwn2", "EVIL!", ("issue", 2))], outs=[_out("XNewOwner", "EVIL!", "transfer", 0)])
    _tx(db, "after", 8, ins=[("XNewOwner", "EVIL!", ("gift", 0))], outs=[_out("XNewOwner", "EVIL!", "transfer", 0), _out("XLater", "EVIL#ONE", "new", 0)])
    db.commit()
    return db, links


def _handles(links, addr):
    return {(r["handle"], r["proof"]) for r in links.labels([addr]).get(addr, [])}


def test_provable_links_only(tmp_path: Path):
    db, links = _chain(tmp_path)
    links.rebuild()
    assert _handles(links, "XRoot") == {("evil", "claim")}
    assert _handles(links, "XOwn1") == {("evil", "owner")}
    assert _handles(links, "XOwn2") == {("evil", "owner")}
    assert _handles(links, "XFee") == {("evil", "cospend")}
    # Issued-to address joins only because it later spent together with XFee.
    assert _handles(links, "XSub") == {("evil", "cospend")}
    # Plain recipients, Launch counterparties, swap partners and later owner-token holders get nothing.
    for addr in ("XBuyer", "XBuyerPay", "XStranger", "XOther", "XNewOwner", "XLater", "XBurn"):
        assert _handles(links, addr) == set(), addr
    # The treasury keeps only its own platform handle.
    assert _handles(links, TREASURY) == {("launch_xfer", "claim")}
    db.close()


def test_conflicting_cluster_gets_no_cospend(tmp_path: Path):
    db, links = _chain(tmp_path)
    db.conn.execute("INSERT INTO identities(handle, asset, address) VALUES('other', 'OTHER', 'XFee2')")
    _tx(db, "mix", 9, ins=[("XFee", None, ("f8", 0)), ("XFee2", None, ("f9", 0))], outs=[_out("XZ")])
    db.commit()
    stats = links.rebuild()
    assert stats["clusters_dropped"] == 1
    assert _handles(links, "XSub") == set()
    assert _handles(links, "XFee2") == {("other", "claim")}
    db.close()


def test_wallet_confirmed_links_from_relay(tmp_path: Path):
    def relay(url):
        if url.endswith("/api/identity/wallet-links"):
            return {"links": [{"address": "XVault1", "handle": "@Evil", "at": 1}, {"address": "XBuyer", "handle": "buyer", "at": 2}]}
        if url.endswith("/api/launch/platform-addresses"):
            return {"addresses": ["XReserveA"]}
        return None

    db, links = _chain(tmp_path, relay)
    links.rebuild()
    assert _handles(links, "XVault1") == {("evil", "wallet")}
    assert _handles(links, "XBuyer") == {("buyer", "wallet")}
    db.close()


def test_api_labels_and_batch(tmp_path: Path):
    db, links = _chain(tmp_path)
    links.rebuild()
    rpc = XCoinRPC(Settings())
    client = TestClient(create_app(Queries(db), Indexer(db, rpc), rpc, links=links))
    body = client.get("/api/identities", params={"addrs": "XSub,XBuyer,XRoot"}).json()
    assert set(body["labels"]) == {"XSub", "XRoot"}
    assert body["labels"]["XSub"][0]["proof"] == "cospend"
    tx = client.get("/api/tx/list").json()
    assert tx["labels"]["XSub"][0]["handle"] == "evil"
    addr = client.get("/api/address/XFee").json()
    assert addr["labels"]["XFee"][0]["handle"] == "evil"
    profile = client.get("/api/handle/evil").json()
    assert {"XRoot", "XOwn1", "XOwn2", "XFee", "XSub"} <= {r["address"] for r in profile["addresses"]}
    db.close()


def test_helpers():
    sig = "30" + "00" * 69 + "01"
    assert sighash_flags(f"{len(sig) // 2:02x}{sig}21" + "02" * 33) == "01"
    assert sighash_flags("") is None
    assert addresses_in({"vin": [{"address": "XA"}], "x": {"issuer": "XB", "name": "Q"}}) == {"XA", "XB"}


def _sig(hashtype: str) -> str:
    sig = "30" + "00" * 8 + hashtype
    return f"{len(sig) // 2:02x}{sig}"


class _BlockRpc:
    """Node without txindex: getrawtransaction fails, getblock answers."""

    connected = True

    def __init__(self, blocks):
        self.blocks = blocks
        self.calls = []

    def try_call(self, method, *params, default=None):
        self.calls.append(method)
        if method == "getblockhash":
            return f"h{params[0]}"
        if method == "getblock":
            return self.blocks.get(params[0], default)
        return default


def test_sighash_read_from_the_block_without_txindex(tmp_path: Path):
    db = Database(tmp_path / "s.db")
    db.conn.execute("INSERT INTO txs(txid, height, n) VALUES('a', 7, 1)")
    db.conn.execute("INSERT INTO txs(txid, height, n) VALUES('b', 7, 2)")
    block = {
        "tx": [
            {"txid": "cb", "vin": [{"coinbase": "00"}]},
            {"txid": "a", "vin": [{"scriptSig": {"hex": _sig("01")}}, {"scriptSig": {"hex": _sig("01")}}]},
            {"txid": "b", "vin": [{"scriptSig": {"hex": _sig("01")}}, {"scriptSig": {"hex": _sig("83")}}]},
        ]
    }
    rpc = _BlockRpc({"h7": block})
    links = AddressLinks(db, rpc)
    assert links._all_sighash_all("a") is True
    assert links._all_sighash_all("b") is False  # read from the same block, no second call
    assert rpc.calls.count("getblock") == 1
    assert "getrawtransaction" not in rpc.calls
    assert db.conn.execute("SELECT COUNT(*) FROM tx_sighash WHERE txid='cb'").fetchone()[0] == 0


def test_indexed_blocks_fill_the_sighash_cache(tmp_path: Path):
    db = Database(tmp_path / "s.db")
    links = AddressLinks(db, None)
    links.remember_block([{"txid": "c", "vin": [{"scriptSig": {"hex": _sig("01")}}]}])
    assert links._all_sighash_all("c") is True


def _op_return_hex(text: str) -> str:
    payload = text.encode()
    return (bytes([0x6A, len(payload)]) + payload).hex()


def _insert_fresh(db, txid, height, n, when, vins, vouts):
    db.conn.execute(
        """
        INSERT INTO txs(txid, height, n, time, coinbase, vin_count, vout_count)
        VALUES(?,?,?,?,0,?,?)
        """,
        (txid, height, n, when, len(vins), len(vouts)),
    )
    for i, (addr, value) in enumerate(vins):
        db.conn.execute(
            """
            INSERT INTO txio(txid, n, direction, address, value, coinbase)
            VALUES(?,?,?,?,?,0)
            """,
            (txid, i, "in", addr, value),
        )
    for i, (addr, value, memo) in enumerate(vouts):
        db.conn.execute(
            """
            INSERT INTO txio(txid, n, direction, address, value, op_return, script_type, coinbase)
            VALUES(?,?,?,?,?,?,?,0)
            """,
            (txid, i, "out", addr, value, memo, "nulldata" if memo else "pubkeyhash"),
        )
    db.conn.execute(
        "INSERT OR REPLACE INTO tx_sighash(txid, all_sighash_all) VALUES(?,1)",
        (txid,),
    )


class _MempoolRpc:
    connected = True

    def __init__(self, txs):
        self.txs = txs

    def try_call(self, method, *args, default=None):
        if method == "getrawmempool":
            return list(self.txs)
        if method == "getrawtransaction":
            return self.txs.get(args[0], default)
        return default


def test_fresh_change_address_shows_on_tx_trades_and_block(tmp_path: Path):
    """A new change address is labeled from the tx that created it, before the timer.

    The snapshot is rebuilt once for the claim, then the fresh trade is inserted
    and not rebuilt by the test. Tx, trades, and block pages must still show the
    handle, and so must a mempool trade that spends the same wallet.
    """
    from explorer.chain import COIN
    from explorer.trades import DEFAULT_LAUNCH_PROCEEDS, LAUNCH_TREASURY

    wallet = "XWalletClaimed111111111111111111"
    change = "XChangeFresh11111111111111111111"
    reserve = "XReserveFresh111111111111111111"
    fresh_in = "XFreshInput1111111111111111111"
    mem_change = "XMemChange111111111111111111111"
    mem_reserve = "XMemReserve11111111111111111111"
    net = 9_910_000_000
    gross = 100 * COIN
    platform_fee = 60_000_000
    buyer_in = gross + COIN
    change_value = buyer_in - platform_fee - net - COIN
    memo = f"XL1|B|OTHER/CHILD|{net}|100"
    now = int(time.time())
    trade_id = "aa" * 32
    coin_id = "bb" * 32
    mem_id = "cc" * 32

    db = Database(tmp_path / "fresh.db")
    links = AddressLinks(db, None, DEFAULT_LAUNCH_PROCEEDS)
    db.conn.execute(
        "INSERT INTO identities(handle, asset, address, txid) VALUES('evil', 'EVIL', ?, 'claim')",
        (wallet,),
    )
    db.conn.execute("INSERT INTO assets(name, kind, units) VALUES('OTHER/CHILD', 'sub', 8)")
    db.commit()
    links.rebuild()
    assert links._built_height == -1
    assert _handles(links, change) == set()

    _insert_fresh(db, "fund", 2, 0, now, [], [(wallet, buyer_in, None)])
    _insert_fresh(
        db,
        trade_id,
        4,
        0,
        now,
        [(wallet, buyer_in)],
        [
            (LAUNCH_TREASURY, platform_fee, None),
            (reserve, net, None),
            (change, change_value, None),
            (None, 0, memo),
        ],
    )
    _insert_fresh(
        db,
        coin_id,
        4,
        1,
        now,
        [(wallet, 1000), (fresh_in, 1000)],
        [(wallet, 2000, None)],
    )
    db.conn.execute(
        "INSERT INTO blocks(height, hash, time, tx_count) VALUES(?,?,?,?)",
        (4, "44" * 32, now, 2),
    )
    db.conn.execute(
        "INSERT INTO utxos(txid, n, address, value, asset, asset_amount, height) VALUES(?,?,?,?,?,?,?)",
        (trade_id, 2, change, 0, "OTHER/CHILD", 1, 4),
    )
    db.conn.execute(
        "INSERT INTO asset_activity(height, txid, name, kind, amount, address) VALUES(?,?,?,?,?,?)",
        (4, trade_id, "OTHER/CHILD", "transfer", 1, change),
    )
    db.commit()
    # The fresh tx is in the index and the handle snapshot is still the old one.
    assert links.stale()
    assert links._built_height == -1

    raw_mem = {
        "txid": mem_id,
        "time": now,
        "vin": [{"txid": "fund", "vout": 0, "scriptSig": {"hex": _sig("01")}}],
        "vout": [
            {"n": 0, "value": platform_fee, "scriptPubKey": {"addresses": [LAUNCH_TREASURY], "type": "pubkeyhash"}},
            {"n": 1, "value": net, "scriptPubKey": {"addresses": [mem_reserve], "type": "pubkeyhash"}},
            {"n": 2, "value": change_value, "scriptPubKey": {"addresses": [mem_change], "type": "pubkeyhash"}},
            {
                "n": 3,
                "value": 0,
                "scriptPubKey": {"hex": _op_return_hex(memo), "type": "nulldata"},
            },
        ],
    }
    rpc = _MempoolRpc({mem_id: raw_mem})
    client = TestClient(create_app(Queries(db), Indexer(db, rpc), rpc, links=links))

    def handle_of(payload, addr):
        rows = (payload.get("labels") or {}).get(addr) or []
        return rows[0]["handle"] if rows else None

    tx = client.get(f"/api/tx/{trade_id}").json()
    assert handle_of(tx, change) == "evil"
    assert handle_of(tx, wallet) == "evil"
    assert handle_of(tx, reserve) is None
    assert handle_of(tx, LAUNCH_TREASURY) is None

    block = client.get("/api/block/4").json()
    assert handle_of(block, change) == "evil"
    assert handle_of(block, fresh_in) == "evil"
    assert change in block["txs"][0]["addresses"]
    assert handle_of(block, reserve) is None
    assert handle_of(block, LAUNCH_TREASURY) is None

    trades = client.get("/api/trades").json()
    assert handle_of(trades, change) == "evil"
    assert handle_of(trades, mem_change) == "evil"
    assert any(item["trader"] == wallet and item["trader_handle"] == "evil" for item in trades["items"])
    assert handle_of(trades, reserve) is None
    assert handle_of(trades, mem_reserve) is None
    assert handle_of(trades, LAUNCH_TREASURY) is None

    addr = client.get(f"/api/address/{change}").json()
    assert handle_of(addr, change) == "evil"
    coin = client.get(f"/api/tx/{coin_id}").json()
    assert handle_of(coin, fresh_in) == "evil"

    mem_tx = client.get(f"/api/tx/{mem_id}").json()
    assert mem_tx["unindexed"] is True
    assert handle_of(mem_tx, mem_change) == "evil"
    assert handle_of(mem_tx, mem_reserve) is None

    pool = client.get("/api/mempool").json()
    assert handle_of(pool, mem_change) == "evil"
    assert mem_change in pool["txs"][0]["addresses"]

    holders = client.get("/api/asset-holders/OTHER/CHILD").json()
    assert handle_of(holders, change) == "evil"
    activity = client.get("/api/asset-activity/OTHER/CHILD").json()
    assert handle_of(activity, change) == "evil"
    db.close()
