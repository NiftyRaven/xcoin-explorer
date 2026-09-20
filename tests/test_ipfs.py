from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.chain import RVN_PREFIX
from explorer.config import Settings
from explorer.db import Database
from explorer.decode import b58encode, ipfs_bytes_to_cid, normalize_ipfs, parse_asset_payload
from explorer.indexer import Indexer
from explorer.ipfs import (
    GATEWAYS,
    attach_ipfs_fields,
    extract_nft_media,
    inspect_cid,
    pinata_view_url,
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
    assert nft["image"]["src"] == pinata_view_url(cid)
    assert nft["animation"]["kind"] == "url"
    assert nft["attributes"][0]["value"] == "gold"


def test_pinata_view_url_is_dedicated_gateway():
    _, cid = _cid0(b"\x33" * 32)
    assert pinata_view_url(cid) == f"https://xfer.mypinata.cloud/ipfs/{cid}"
    assert GATEWAYS[0] == "https://xfer.mypinata.cloud/ipfs/"
    assert GATEWAYS[1] == "https://gateway.pinata.cloud/ipfs/"


def test_inspect_url_prefers_pinata(monkeypatch):
    _, cid = _cid0(b"\x44" * 32)

    def fake_fetch(c, limit, peek=False):
        return b"\xff\xd8\xff\xe0", "image/jpeg", pinata_view_url(c), True

    monkeypatch.setattr("explorer.ipfs._fetch", fake_fetch)
    out = inspect_cid(cid)
    assert out["url"] == pinata_view_url(cid)
    assert out["kind"] == "image"
    assert out["gateways"][0] == pinata_view_url(cid)


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
