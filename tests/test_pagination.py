"""Server-side paging: every long list returns one page plus the full total."""

from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.config import Settings
from explorer.db import Database
from explorer.indexer import Indexer
from explorer.queries import Queries
from explorer.rpc import XCoinRPC

A = "XmLv1ZYu8qMsGTsWvD9N7C7AFcK844nHwF"


def _seed(db: Database) -> None:
    c = db.conn
    c.executemany(
        "INSERT INTO blocks(height,hash,time,tx_count) VALUES(?,?,?,1)",
        [(h, f"{h:064x}", 1_700_000_000 + h * 60) for h in range(130)],
    )
    c.executemany(
        "INSERT INTO lottery_wins(height,rank,address,amount,xaccount) VALUES(?,0,?,?,?)",
        [(h, A, 5000, f"user{h % 60:02d}") for h in range(1, 130)],
    )
    c.executemany(
        "INSERT INTO txs(txid,height,n,time) VALUES(?,?,0,?)",
        [(f"a{i:063x}", i, 1_700_000_000 + i) for i in range(120)],
    )
    c.executemany(
        "INSERT INTO txio(txid,n,direction,address,value) VALUES(?,0,'out',?,?)",
        [(f"a{i:063x}", A, 100 + i) for i in range(120)],
    )
    c.executemany(
        "INSERT INTO utxos(txid,n,address,value,asset,asset_amount) VALUES(?,?,?,?,?,?)",
        [(f"b{i:063x}", 0, f"Xaddr{i:03d}", 1000 + i, None, 0) for i in range(110)]
        + [(f"c{i:063x}", 0, f"Xaddr{i:03d}", 0, "ROOT", 10 + i) for i in range(70)],
    )
    c.executemany(
        "INSERT INTO assets(name,kind,amount,created_height,x_handle) VALUES(?,?,1,?,?)",
        [("ROOT", "root", 0, "user00")]
        + [(f"ROOT/SUB{i:02d}", "sub" if i % 2 else "unique", i + 1, None) for i in range(60)],
    )
    c.executemany(
        "INSERT INTO asset_activity(height,txid,name,kind,amount,address) VALUES(?,?,'ROOT','transfer',1,?)",
        [(i, f"d{i:063x}", f"Xaddr{i:03d}") for i in range(55)],
    )
    c.commit()


def _client(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    _seed(db)
    rpc = XCoinRPC(Settings())
    return db, TestClient(create_app(Queries(db), Indexer(db, rpc), rpc))


def _walk(client, path: str, key: str = "items", size: int = 25) -> tuple[list, int]:
    first = client.get(f"{path}{'&' if '?' in path else '?'}limit={size}&offset=0").json()
    total = first.get("total", first.get("tx_total"))
    got = list(first[key])
    offset = size
    while offset < total:
        page = client.get(f"{path}{'&' if '?' in path else '?'}limit={size}&offset={offset}").json()
        assert page.get("total", page.get("tx_total")) == total
        got.extend(page[key])
        offset += size
    return got, total


def test_blocks_pages_cover_every_block_once(tmp_path: Path):
    db, client = _client(tmp_path)
    items, total = _walk(client, "/api/blocks")
    assert total == 130
    assert [b["height"] for b in items] == list(range(129, -1, -1))
    page = client.get("/api/blocks?limit=25&offset=50").json()
    assert (page["limit"], page["offset"]) == (25, 50)
    assert page["items"][0]["height"] == 79
    # The old cursor form still works.
    assert client.get("/api/blocks?limit=5&before=10").json()["items"][0]["height"] == 9
    db.close()


def test_address_history_pages_and_total(tmp_path: Path):
    db, client = _client(tmp_path)
    items, total = _walk(client, f"/api/address/{A}", key="txs")
    assert total == 120
    assert len({t["txid"] for t in items}) == 120
    assert [t["height"] for t in items] == list(range(119, -1, -1))
    assert all("tx_total" not in t for t in items)
    past = client.get(f"/api/address/{A}?limit=25&offset=500").json()
    assert past["txs"] == [] and past["tx_total"] == 120
    assert client.get("/api/address/Xnobody").json()["tx_total"] == 0
    db.close()


def test_assets_filters_apply_before_paging(tmp_path: Path):
    db, client = _client(tmp_path)
    items, total = _walk(client, "/api/assets?kind=unique", size=7)
    assert total == 30 and len(items) == 30
    assert all(a["kind"] == "unique" for a in items)
    found = client.get("/api/assets?q=sub0&limit=3&offset=3").json()
    assert found["total"] == 10
    assert [a["name"] for a in found["items"]] == ["ROOT/SUB06", "ROOT/SUB05", "ROOT/SUB04"]
    db.close()


def test_rich_list_pages(tmp_path: Path):
    db, client = _client(tmp_path)
    items, total = _walk(client, "/api/rich")
    assert total == 110
    balances = [r["balance"] for r in items]
    assert balances == sorted(balances, reverse=True) and len(set(r["address"] for r in items)) == 110
    assert client.get("/api/rich?limit=25&offset=999").json() == {
        "total": 110, "limit": 25, "offset": 999, "items": [], "labels": {}
    }
    db.close()


def test_asset_holders_and_activity_pages(tmp_path: Path):
    db, client = _client(tmp_path)
    holders, total = _walk(client, "/api/asset-holders/root")
    assert total == 70 and len({h["address"] for h in holders}) == 70
    activity, total = _walk(client, "/api/asset-activity/ROOT", size=10)
    assert total == 55 and [a["height"] for a in activity] == list(range(54, -1, -1))
    assert client.get("/api/asset-holders/NOPE").status_code == 404
    a = client.get("/api/asset/ROOT").json()
    assert a["holder_count"] == 70 and a["activity_total"] == 55 and len(a["holders"]) == 70
    db.close()


def test_members_scope_and_search_page_server_side(tmp_path: Path):
    db, client = _client(tmp_path)
    items, total = _walk(client, "/api/lottery/members?scope=all")
    assert total == 60 and len({m["handle"] for m in items}) == 60
    found = client.get("/api/lottery/members?scope=all&q=user1&limit=4&offset=8").json()
    assert found["total"] == 10 and len(found["items"]) == 2
    assert found["items"][0]["handle"].startswith("user1")
    db.close()


def test_limits_are_capped_and_bad_offsets_clamp(tmp_path: Path):
    db, client = _client(tmp_path)
    assert len(client.get("/api/blocks?limit=100000").json()["items"]) == 100
    assert len(client.get("/api/rich?limit=100000").json()["items"]) == 100
    assert len(client.get(f"/api/address/{A}?limit=100000").json()["txs"]) == 100
    assert len(client.get("/api/asset-holders/ROOT?limit=100000").json()["items"]) == 70
    assert client.get("/api/assets?limit=0&offset=-5").json()["offset"] == 0
    assert client.get("/api/assets?limit=0").json()["limit"] == 1
    db.close()


def test_ui_pages_long_lists_from_the_hash(tmp_path: Path):
    db, client = _client(tmp_path)
    js = client.get("/static/js/app.js").text
    assert "limit=400" not in js and 'api("/assets?limit=80")' not in js and 'api("/rich?limit=50")' not in js
    for page in ("pageMembers", "pageBlocks", "pageAssets", "pageRich", "pageAddress", "pageAsset"):
        body = js[js.index(f"async function {page}(") :]
        body = body[: body.index("\nasync function ", 1)] if "\nasync function " in body[1:] else body
        assert "pageState(" in body and "pagerHtml(" in body and "bindPagers()" in body, page
    assert "const PAGE_SIZES = [25, 50, 100];" in js
    # Routes accept ?page=&size= on every paged list.
    for route in ("blocks", "assets", "rich", "members"):
        assert f"[/^#\\/{route}(?:\\?.*)?$/" in js, route
    assert "/^#\\/address\\/([^?]+)(?:\\?.*)?$/" in js and "/^#\\/asset\\/([^?]+)(?:\\?.*)?$/" in js
    home = client.get("/").text
    assert "app.js?v=explorer-public-1-pages" in home and "app.css?v=pages-1" in home
    db.close()
