import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.chain import RVN_PREFIX
from explorer.config import Settings
from explorer.db import Database
from explorer.decode import b58encode, ipfs_bytes_to_cid, normalize_ipfs, parse_asset_payload
from explorer.indexer import Indexer
from explorer.ipfs import (
    GATEWAYS,
    JSON_FETCH_TIMEOUT,
    JSON_MAX_BYTES,
    META_CACHE_MAX,
    _is_gateway_miss,
    _presentation,
    attach_ipfs_fields,
    clear_metadata_cache,
    extract_nft_media,
    fetch_content,
    inspect_asset_ipfs,
    inspect_cid,
    ipfs_content_url,
    parse_ipfs_location,
    pinata_view_url,
    safe_http_url,
    sanitize_svg,
    valid_cid,
)
from explorer.queries import Queries
from explorer.rpc import XCoinRPC


def _cid0(digest32: bytes | None = None) -> tuple[bytes, str]:
    raw = bytes([0x12, 0x20]) + (digest32 if digest32 is not None else bytes(32))
    return raw, b58encode(raw)


def test_normalize_ipfs_cid_and_hex():
    raw, cid = _cid0()
    assert ipfs_bytes_to_cid(raw) == cid
    assert normalize_ipfs(cid) == cid
    assert normalize_ipfs("ipfs://" + cid) == cid
    assert normalize_ipfs("ipfs://ipfs/" + cid) == cid
    assert normalize_ipfs("ipfs://ipfs/" + cid + "/file.png") == cid
    assert normalize_ipfs("/ipfs/" + cid + "/file.png") == cid
    assert normalize_ipfs("https://xfer.mypinata.cloud/ipfs/" + cid + "/file.png") == cid
    assert normalize_ipfs("https://evil.example/ipfs/" + cid) == cid
    assert normalize_ipfs(raw.hex()) == cid
    assert normalize_ipfs("22" + raw.hex()) == cid
    assert normalize_ipfs("not-a-hash") == ""
    assert valid_cid(cid)


def test_parse_new_asset_ipfs_payload():
    raw, cid = _cid0(b"\xab" * 32)
    name = b"TESTNFT"
    amount = (100_000_000).to_bytes(8, "little", signed=True)
    payload = (
        RVN_PREFIX
        + b"n"
        + bytes([len(name)])
        + name
        + amount
        + bytes([0, 0, 1, 34])
        + raw
    )
    parsed = parse_asset_payload(payload)
    assert parsed is not None
    assert parsed.name == "TESTNFT"
    assert parsed.ipfs == cid


def test_attach_ipfs_prefers_rpc_hash():
    raw, cid = _cid0(b"\xcd" * 32)
    asset = attach_ipfs_fields({"ipfs": raw.hex()}, {"has_ipfs": 1, "ipfs_hash": cid})
    assert asset["ipfs_cid"] == cid
    assert asset["has_ipfs"] is True
    assert asset["ipfs_gateways"][0] == pinata_view_url(cid)
    assert GATEWAYS[0] == "https://xfer.mypinata.cloud/ipfs/"


def test_extract_nft_media_fields():
    raw, cid = _cid0(b"\x11" * 32)
    nft = extract_nft_media(
        {
            "name": "Piece",
            "description": "On-chain art",
            "image": "ipfs://" + cid,
            "animation_url": "https://example.com/clip.mp4",
            "attributes": [{"trait_type": "color", "value": "gold"}],
        }
    )
    assert nft["name"] == "Piece"
    assert nft["image"]["cid"] == cid
    assert nft["image"]["src"] == pinata_view_url(cid)
    assert nft["image"]["path"] == ""
    assert nft["animation"]["kind"] == "url"
    assert nft["attributes"][0]["value"] == "gold"


def test_pinata_view_url_is_dedicated_gateway():
    _, cid = _cid0(b"\x33" * 32)
    assert pinata_view_url(cid) == f"https://xfer.mypinata.cloud/ipfs/{cid}"
    assert GATEWAYS == ("https://xfer.mypinata.cloud/ipfs/",)
    assert _is_gateway_miss(
        "text/plain;charset=UTF-8",
        b"The owner of this gateway does not have this content pinned to their Pinata account. - ERR_ID:00006",
    )
    assert not _is_gateway_miss("image/gif", b"GIF89a" + b"\x00" * 16)


def test_inspect_url_is_content_proxy_with_dedicated_first(monkeypatch):
    _, cid = _cid0(b"\x44" * 32)

    def fake_fetch(c, limit, peek=False):
        return b"\xff\xd8\xff\xe0", "image/jpeg", pinata_view_url(c), True

    monkeypatch.setattr("explorer.ipfs._fetch", fake_fetch)
    out = inspect_cid(cid)
    assert out["url"] == ipfs_content_url(cid)
    assert out["url"] != pinata_view_url(cid)
    assert out["kind"] == "image"
    assert out["gateways"][0] == pinata_view_url(cid)
    assert GATEWAYS[0] == "https://xfer.mypinata.cloud/ipfs/"


class _FakeStream:
    def __init__(self, status, content_type, body):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self._body


class _FakeClient:
    def __init__(self, routes, calls):
        self._routes = routes
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url):
        self._calls.append(url)
        for prefix, payload in self._routes:
            if url.startswith(prefix):
                return _FakeStream(*payload)
        return _FakeStream(404, "text/plain", b"missing")


def _patch_gateways(monkeypatch, routes):
    calls: list[str] = []

    def factory(*_args, **_kwargs):
        return _FakeClient(routes, calls)

    monkeypatch.setattr("explorer.ipfs.httpx.Client", factory)
    return calls


def test_fetch_does_not_leave_our_gateway(monkeypatch):
    _, cid = _cid0(b"\x55" * 32)
    calls = _patch_gateways(
        monkeypatch,
        [
            (
                "https://xfer.mypinata.cloud/",
                (
                    403,
                    "text/plain;charset=UTF-8",
                    b"The owner of this gateway does not have this content pinned. ERR_ID:00006",
                ),
            ),
            ("https://gateway.pinata.cloud/", (200, "image/gif", b"GIF89a" + b"\x00" * 8)),
            ("https://ipfs.io/", (200, "image/gif", b"GIF89a" + b"\x00" * 8)),
        ],
    )
    with pytest.raises(ValueError, match="IPFS fetch failed"):
        fetch_content(cid)
    assert calls
    assert all(u.startswith("https://xfer.mypinata.cloud/") for u in calls)
    assert not any("gateway.pinata.cloud" in u or "ipfs.io" in u for u in calls)


def test_fetch_refuses_dedicated_html_miss_without_another_host(monkeypatch):
    _, cid = _cid0(b"\x66" * 32)
    calls = _patch_gateways(
        monkeypatch,
        [
            (
                "https://xfer.mypinata.cloud/",
                (
                    200,
                    "text/html",
                    b"<html>The owner of this gateway does not have this content pinned to their Pinata account. ERR_ID:00006</html>",
                ),
            ),
            ("https://gateway.pinata.cloud/", (200, "image/gif", b"GIF89a" + b"\x01" * 8)),
        ],
    )
    with pytest.raises(ValueError, match="IPFS fetch failed"):
        fetch_content(cid)
    assert calls
    assert all("xfer.mypinata.cloud" in u for u in calls)


def test_inspect_stops_when_our_gateway_refuses(monkeypatch):
    clear_metadata_cache()
    _, cid = _cid0(b"\x77" * 32)
    calls = _patch_gateways(
        monkeypatch,
        [
            (
                "https://xfer.mypinata.cloud/",
                (
                    403,
                    "text/plain;charset=UTF-8",
                    b"The owner of this gateway does not have this content pinned. ERR_ID:00006",
                ),
            ),
            ("https://gateway.pinata.cloud/", (200, "image/jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 16)),
        ],
    )
    with pytest.raises(ValueError, match="IPFS fetch failed"):
        inspect_cid(cid)
    assert all(u.startswith("https://xfer.mypinata.cloud/") for u in calls)
    clear_metadata_cache()


def test_valid_cid_accepts_v0_46_and_v1_only():
    cid0 = "Qm" + "1" * 44
    assert len(cid0) == 46
    assert valid_cid(cid0)
    assert not valid_cid("Qm" + "1" * 43)
    assert not valid_cid("Qm" + "1" * 45)
    assert not valid_cid("Qm" + "0" * 44)
    cid1 = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
    assert valid_cid(cid1)
    assert not valid_cid("javascript:alert(1)")
    assert not valid_cid("")


def test_safe_http_url_allows_http_only():
    assert safe_http_url("https://xferchain.net/a") == "https://xferchain.net/a"
    assert safe_http_url("http://example.com") == "http://example.com"
    assert safe_http_url("javascript:alert(1)") == ""
    assert safe_http_url("https://user:secret@example.com/a") == ""
    assert safe_http_url("<a href='https://example.com'>x</a>") == ""


def test_extract_nft_media_strips_html_and_keeps_http_link():
    raw, cid = _cid0(b"\x12" * 32)
    nft = extract_nft_media(
        {
            "name": "<b>Hi</b>",
            "description": "<script>alert(1)</script>Hello",
            "image": "ipfs://" + cid,
            "external_url": "javascript:alert(1)",
            "website_url": "https://example.com/token",
            "attributes": [{"trait_type": "<i>Color</i>", "value": 3}],
            "contract_address": "nope",
        }
    )
    assert nft["name"] == "Hi"
    assert "<" not in nft["description"]
    assert "Hello" in nft["description"]
    assert nft["external_url"] == "https://example.com/token"
    assert nft["image"]["cid"] == cid
    assert nft["attributes"] == [{"trait_type": "Color", "value": "3"}]


def test_inspect_json_metadata_is_plain_cached_and_capped(monkeypatch):
    clear_metadata_cache()
    _, cid = _cid0(b"\x81" * 32)
    _, image = _cid0(b"\x82" * 32)
    body = json.dumps(
        {
            "name": "<b>Gold X</b>",
            "description": "No mining.\n<script>alert(1)</script>One minute.",
            "image_url": image,
            "external_url": "https://xferchain.net/x",
            "attributes": [{"trait_type": "Color", "value": "Gold"}],
            "contract_address": "do-not-show",
        }
    ).encode()
    calls = {"n": 0}

    def fake_fetch(c, limit, peek=False):
        calls["n"] += 1
        assert c == cid
        assert peek is True
        assert limit == JSON_MAX_BYTES
        assert limit == 256 * 1024
        return body, "application/json", pinata_view_url(c), False

    monkeypatch.setattr("explorer.ipfs._fetch", fake_fetch)
    out = inspect_cid(cid)
    again = inspect_cid(cid)
    assert calls["n"] == 1
    assert again["view"]["name"] == out["view"]["name"] == "Gold X"
    assert "<" not in out["view"]["description"]
    assert "One minute." in out["view"]["description"]
    assert out["view"]["image"]["cid"] == image
    assert out["view"]["image"]["src"] == pinata_view_url(image)
    assert out["presentation"]["type"] == "image"
    assert out["presentation"]["urls"][0] == pinata_view_url(image)
    assert all("ipfs.io" not in u and "gateway.pinata.cloud" not in u for u in out["presentation"]["urls"])
    assert out["view"]["external_url"] == "https://xferchain.net/x"
    assert out["view"]["attributes"][0] == {"trait_type": "Color", "value": "Gold"}
    assert "<b>" in out["raw_json"]
    assert "contract" not in out["raw_json"]
    clear_metadata_cache()


def test_inspect_oversize_json_degrades_quietly(monkeypatch):
    clear_metadata_cache()
    _, cid = _cid0(b"\x83" * 32)
    blob = b'{"name":"too-big"}' + b" " * (300 * 1024)
    _patch_gateways(
        monkeypatch,
        [("https://xfer.mypinata.cloud/", (200, "application/json", blob))],
    )
    out = inspect_cid(cid)
    assert out["kind"] == "json"
    assert out["truncated"] is True
    assert "view" not in out
    assert "metadata" not in out
    clear_metadata_cache()


def test_invalid_cid_is_rejected_before_fetch(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("should not fetch")

    monkeypatch.setattr("explorer.ipfs.httpx.Client", boom)
    with pytest.raises(ValueError, match="invalid IPFS CID"):
        inspect_cid("nope")
    with pytest.raises(ValueError, match="invalid IPFS CID"):
        inspect_cid("Qm" + "1" * 10)


def test_inspect_timeout_does_not_hang(monkeypatch):
    clear_metadata_cache()
    _, cid = _cid0(b"\x84" * 32)

    class _TimeoutClient:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def stream(self, _method, _url):
            raise httpx.TimeoutException("timed out")

    def factory(*_args, **kwargs):
        assert kwargs.get("timeout") == JSON_FETCH_TIMEOUT
        return _TimeoutClient()

    monkeypatch.setattr("explorer.ipfs.httpx.Client", factory)
    with pytest.raises(ValueError, match="IPFS fetch failed"):
        inspect_cid(cid)


def test_explorer_ui_clips_ids_and_renders_metadata_safely():
    root = Path(__file__).resolve().parents[1]
    css = (root / "web" / "css" / "app.css").read_text(encoding="utf-8")
    js = (root / "web" / "js" / "app.js").read_text(encoding="utf-8")
    assert "overflow-wrap: anywhere" in css
    assert ".gw-chip" in css
    assert "white-space: nowrap" in css
    assert "gatewayChipsHtml" in js
    assert "Cloudflare" in js
    assert 'class="copy-btn"' in js
    assert "mono-clip" in js
    assert "renderMetadataCard" in js
    assert "noopener noreferrer" in js
    assert "Raw JSON" in js
    assert "esc(raw)" in js
    assert "esc(description)" in js
    assert "muted autoplay loop playsinline" in js
    assert "Open audio" in js
    assert "<audio" not in js
    assert "This file did not load." in js
    assert "data-asset-name" in js


def test_asset_api_includes_ipfs_and_rejects_bad_cid(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    rpc = XCoinRPC(Settings())
    indexer = Indexer(db, rpc)
    app = create_app(Queries(db), indexer, rpc)
    client = TestClient(app)
    _, cid = _cid0(b"\x22" * 32)
    db.conn.execute(
        """
        INSERT INTO assets(name, kind, amount, units, reissuable, ipfs, created_height, created_txid)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        ("TESTNFT", "unique", 100_000_000, 0, 0, cid, 12, "ab" * 32),
    )
    db.conn.commit()
    r = client.get("/api/asset/TESTNFT")
    assert r.status_code == 200
    body = r.json()
    assert body["ipfs_cid"] == cid
    assert body["has_ipfs"] is True
    assert body["ipfs_gateways"][0] == f"https://xfer.mypinata.cloud/ipfs/{cid}"
    bad = client.get("/api/ipfs/inspect", params={"cid": "nope"})
    assert bad.status_code == 400
    db.conn.execute(
        "INSERT INTO assets(name, kind, amount, units, reissuable, created_height) VALUES(?,?,?,?,?,?)",
        ("ROOT/CHILD", "sub", 1, 0, 1, 3),
    )
    db.conn.commit()
    child = client.get("/api/asset/ROOT/CHILD")
    assert child.status_code == 200
    assert child.json()["name"] == "ROOT/CHILD"
    db.close()


def test_parse_media_locations_and_schemas():
    _, cid = _cid0(b"\x91" * 32)
    _, image = _cid0(b"\x92" * 32)
    _, anim = _cid0(b"\x93" * 32)
    cid1 = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
    assert parse_ipfs_location("ipfs://ipfs/" + cid + "/a.png") == (cid, "a.png")
    assert parse_ipfs_location("ipfs://" + cid1) == (cid1, "")
    assert parse_ipfs_location("https://ipfs.io/ipfs/" + cid + "/b.png") == (cid, "b.png")
    assert parse_ipfs_location("https://user:pw@ipfs.io/ipfs/" + cid) == ("", "")
    assert parse_ipfs_location("../etc/passwd") == ("", "")
    assert parse_ipfs_location("javascript:alert(1)") == ("", "")
    nft = extract_nft_media(
        {
            "schema": "xcoin-asset-1",
            "name": "<b>Castle</b>",
            "description": "<script>no</script>Walls",
            "image_url": "ipfs://ipfs/" + image + "/keep.png",
            "website_url": "https://example.com/castle",
            "external_url": "https://example.com/token",
            "animation_url": "ipfs://" + anim + "/walk.mp4",
            "attributes": [{"trait_type": "<i>Place</i>", "value": "Celje"}],
            "contract_address": "nope",
        }
    )
    assert nft["name"] == "Castle"
    assert "<" not in nft["description"]
    assert "Walls" in nft["description"]
    assert nft["image"]["cid"] == image
    assert nft["image"]["path"] == "keep.png"
    assert nft["image"]["src"].startswith("https://xfer.mypinata.cloud/ipfs/")
    assert nft["website_url"] == "https://example.com/castle"
    assert nft["external_url"] == "https://example.com/token"
    assert nft["animation"]["path"] == "walk.mp4"
    assert nft["attributes"] == [{"trait_type": "Place", "value": "Celje"}]
    shown = _presentation({"kind": "json", "cid": cid, "nft": nft}, "CASTLE")
    assert shown["type"] == "video"
    assert "walk.mp4" in shown["urls"][0]
    assert "example.com" not in " ".join(shown["urls"] + shown["poster"])
    assert all(
        u.startswith("https://xfer.mypinata.cloud/ipfs/") or u.startswith("https://xferchain.net/api/assets/ipfs/media/")
        for u in shown["urls"] + shown["poster"]
    )
    foreign = extract_nft_media(
        {"name": "X", "image": "https://evil.example/a.png", "animation_url": "https://evil.example/a.mp4"}
    )
    assert foreign["image"]["kind"] == "url"
    assert _presentation({"kind": "json", "cid": cid, "nft": foreign})["urls"] == []


def test_svg_bytes_are_sanitized(monkeypatch):
    _, cid = _cid0(b"\x94" * 32)
    raw = (
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
        b"<script>alert(1)</script><rect width=\"10\" height=\"10\"/></svg>"
    )
    cleaned = sanitize_svg(raw)
    assert b"<script" not in cleaned.lower()
    assert b"onload" not in cleaned.lower()
    assert b"<svg" in cleaned.lower()

    def fake_fetch(*_args, **_kwargs):
        return raw, "image/svg+xml", pinata_view_url(cid), False

    monkeypatch.setattr("explorer.ipfs._fetch", fake_fetch)
    body, content_type = fetch_content(cid)
    assert content_type == "image/svg+xml"
    assert b"script" not in body.lower()
    assert b"onload" not in body.lower()


def test_directory_cid_resolves_metadata_json(monkeypatch):
    clear_metadata_cache()
    _, cid = _cid0(b"\x95" * 32)
    meta = json.dumps(
        {
            "schema": "xcoin-asset-1",
            "name": "Folder",
            "description": "Inside a directory",
            "image_url": "art.png",
            "website_url": "https://example.com/folder",
            "attributes": [{"trait_type": "Color", "value": "Gold"}],
        }
    ).encode()
    dag = json.dumps(
        {
            "Links": [
                {"Hash": {"/": cid}, "Name": "metadata.json", "Tsize": 20},
                {"Hash": {"/": cid}, "Name": "art.png", "Tsize": 20},
                {"Hash": {"/": cid}, "Name": "../secret", "Tsize": 1},
            ]
        }
    ).encode()
    html = b"<html><body><a href='art.png'>art.png</a></body></html>"

    class _DirClient:
        def __init__(self, calls):
            self._calls = calls

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def stream(self, _method, url):
            self._calls.append(url)
            if "format=dag-json" in url:
                return _FakeStream(200, "application/vnd.ipld.dag-json", dag)
            if url.endswith("/metadata.json"):
                return _FakeStream(200, "application/json", meta)
            if "evil.example" in url or "ipfs.io" in url:
                raise AssertionError(url)
            return _FakeStream(200, "text/html", html)

    calls: list[str] = []

    def factory(*_args, **_kwargs):
        return _DirClient(calls)

    monkeypatch.setattr("explorer.ipfs.httpx.Client", factory)
    out = inspect_cid(cid)
    assert out["kind"] == "json"
    assert out["view"]["name"] == "Folder"
    assert out["view"]["image"]["path"] == "art.png"
    assert out["view"]["image"]["cid"] == cid
    assert out["view"]["attributes"][0]["value"] == "Gold"
    assert out["presentation"]["type"] == "image"
    assert out["presentation"]["urls"][0] == f"https://xfer.mypinata.cloud/ipfs/{cid}/art.png"
    assert all("xfer.mypinata.cloud" in u or "xferchain.net" in u for u in out["presentation"]["urls"])
    assert all("evil" not in u and "ipfs.io" not in u for u in calls)
    clear_metadata_cache()


def test_relay_is_preferred_and_video_is_sniffed(monkeypatch):
    clear_metadata_cache()
    _, cid = _cid0(b"\xa1" * 32)
    _, image = _cid0(b"\xa2" * 32)
    calls: list[str] = []

    def fake_get(url, limit, peek, timeout):
        calls.append(url)
        assert url.startswith("https://xferchain.net/api/")
        assert "name=ASSET" in url
        assert limit == JSON_MAX_BYTES
        body = json.dumps(
            {
                "cid": cid,
                "kind": "json",
                "name": "<b>Gold</b>",
                "rawJson": json.dumps(
                    {
                        "name": "<b>Gold</b>",
                        "description": "Safe",
                        "image_url": "ipfs://ipfs/" + image + "/cover.png",
                        "website_url": "https://example.com/g",
                        "attributes": [{"trait_type": "Color", "value": "Gold"}],
                    }
                ),
            }
        ).encode()
        return body, "application/json", url, False

    monkeypatch.setattr("explorer.ipfs._http_get", fake_get)
    out = inspect_asset_ipfs(cid, "ASSET")
    again = inspect_asset_ipfs(cid, "ASSET")
    assert len(calls) == 1
    assert again["view"]["name"] == "Gold"
    assert "<" not in out["view"]["name"]
    assert out["view"]["image"]["path"] == "cover.png"
    assert out["presentation"]["type"] == "image"
    assert "cover.png" in out["presentation"]["urls"][0]
    assert out["presentation"]["urls"][0].startswith("https://xfer.mypinata.cloud/ipfs/")
    clear_metadata_cache()

    def fake_image(url, limit, peek, timeout):
        body = json.dumps({"cid": cid, "kind": "image", "imageCid": cid}).encode()
        return body, "application/json", url, False

    monkeypatch.setattr("explorer.ipfs._http_get", fake_image)
    monkeypatch.setattr("explorer.ipfs._head_content_type", lambda _c: "video/mp4")
    video = inspect_asset_ipfs(cid, "CLIP")
    assert video["kind"] == "video"
    assert video["presentation"]["type"] == "video"
    clear_metadata_cache()


def test_metadata_cache_stays_bounded(monkeypatch):
    clear_metadata_cache()
    calls = {"n": 0}

    def fake_fetch(c, limit, peek=False, path=""):
        calls["n"] += 1
        return b'{"name":"n"}', "application/json", pinata_view_url(c), False

    monkeypatch.setattr("explorer.ipfs._fetch", fake_fetch)
    cids = []
    for i in range(META_CACHE_MAX + 2):
        _, cid = _cid0(i.to_bytes(32, "big"))
        cids.append(cid)
        inspect_cid(cid)
    after = calls["n"]
    inspect_cid(cids[-1])
    inspect_cid(cids[0])
    assert calls["n"] == after + 1
    clear_metadata_cache()


def test_fetch_reads_our_gateway(monkeypatch):
    _, cid = _cid0(b"\x56" * 32)
    gif = b"GIF89a" + b"\x02" * 16
    calls = _patch_gateways(monkeypatch, [("https://xfer.mypinata.cloud/", (200, "image/gif", gif))])
    body, content_type = fetch_content(cid)
    assert body == gif
    assert content_type == "image/gif"
    assert calls == [pinata_view_url(cid)]


def test_ipfs_routes_require_the_assets_hash(tmp_path: Path, monkeypatch):
    clear_metadata_cache()
    db = Database(tmp_path / "g.db")
    _, cid = _cid0(b"\x96" * 32)
    _, other = _cid0(b"\x97" * 32)
    db.conn.execute(
        "INSERT INTO assets(name, kind, amount, units, reissuable, ipfs, created_height) VALUES(?,?,?,?,?,?,?)",
        ("TESTNFT", "unique", 1, 0, 0, cid, 1),
    )
    db.conn.commit()

    class _Rpc:
        connected = True

        def __init__(self):
            self.calls = []

        def try_call(self, method, *params, default=None):
            self.calls.append((method, params))
            if method == "getassetdata" and params and params[0] == "TESTNFT":
                return {"has_ipfs": 1, "ipfs_hash": cid}
            return default

    rpc = _Rpc()
    fetched = {"n": 0, "open": None}

    def fake_inspect(c, name=""):
        fetched["n"] += 1
        fetched["open"] = db.open_connection_count()
        return {"cid": c, "kind": "image", "presentation": {"type": "image", "urls": [pinata_view_url(c)], "poster": []}}

    monkeypatch.setattr("explorer.api.inspect_asset_ipfs", fake_inspect)
    client = TestClient(create_app(Queries(db), Indexer(db, rpc), rpc))
    db.discard_thread_connection()
    denied = client.get("/api/ipfs/inspect", params={"cid": other, "name": "TESTNFT"})
    assert denied.status_code == 400
    assert fetched["n"] == 0
    assert any(call[0] == "getassetdata" for call in rpc.calls)
    ok = client.get("/api/ipfs/inspect", params={"cid": cid, "name": "TESTNFT"})
    assert ok.status_code == 200
    assert fetched["open"] == 0
    assert ok.json()["kind"] == "image"
    blocked = client.get(f"/api/ipfs/content/{other}", params={"name": "TESTNFT"})
    assert blocked.status_code == 400

    offline = XCoinRPC(Settings())
    assert offline.connected is False
    offline_client = TestClient(create_app(Queries(db), Indexer(db, offline), offline))
    saved = offline_client.get("/api/ipfs/inspect", params={"cid": cid, "name": "TESTNFT"})
    assert saved.status_code == 200
    missing = offline_client.get("/api/ipfs/inspect", params={"cid": other, "name": "TESTNFT"})
    assert missing.status_code == 400
    db.close()
    clear_metadata_cache()
