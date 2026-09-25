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
    _is_gateway_miss,
    attach_ipfs_fields,
    clear_metadata_cache,
    extract_nft_media,
    fetch_content,
    inspect_cid,
    ipfs_content_url,
    pinata_view_url,
    safe_http_url,
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
    assert normalize_ipfs("/ipfs/" + cid + "/file.png") == cid
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
    assert nft["image"]["src"] == ipfs_content_url(cid)
    assert nft["image"]["src"] != pinata_view_url(cid)
    assert nft["animation"]["kind"] == "url"
    assert nft["attributes"][0]["value"] == "gold"


def test_pinata_view_url_is_dedicated_gateway():
    _, cid = _cid0(b"\x33" * 32)
    assert pinata_view_url(cid) == f"https://xfer.mypinata.cloud/ipfs/{cid}"
    assert GATEWAYS[0] == "https://xfer.mypinata.cloud/ipfs/"
    assert GATEWAYS[1] == "https://gateway.pinata.cloud/ipfs/"
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


def test_fetch_skips_dedicated_pinata_403_and_uses_public_gateway(monkeypatch):
    _, cid = _cid0(b"\x55" * 32)
    gif = b"GIF89a" + b"\x00" * 32
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
            ("https://gateway.pinata.cloud/", (200, "image/gif", gif)),
        ],
    )
    body, content_type = fetch_content(cid)
    assert body.startswith(b"GIF89a")
    assert content_type == "image/gif"
    assert calls[0].startswith("https://xfer.mypinata.cloud/ipfs/")
    assert any(u.startswith("https://gateway.pinata.cloud/ipfs/") for u in calls)


def test_fetch_skips_dedicated_200_html_miss(monkeypatch):
    _, cid = _cid0(b"\x66" * 32)
    gif = b"GIF89a" + b"\x01" * 24
    _patch_gateways(
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
            ("https://gateway.pinata.cloud/", (200, "image/gif", gif)),
        ],
    )
    body, content_type = fetch_content(cid)
    assert body == gif
    assert content_type == "image/gif"


def test_inspect_falls_through_when_dedicated_refuses(monkeypatch):
    _, cid = _cid0(b"\x77" * 32)
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    _patch_gateways(
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
            ("https://gateway.pinata.cloud/", (200, "image/jpeg", jpeg)),
        ],
    )
    out = inspect_cid(cid)
    assert out["kind"] == "image"
    assert out["url"] == ipfs_content_url(cid)
    assert out["source"].startswith("https://gateway.pinata.cloud/ipfs/")
    assert out["gateways"][0] == pinata_view_url(cid)


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
    assert out["view"]["image"]["src"] == ipfs_content_url(image)
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
    db.close()
