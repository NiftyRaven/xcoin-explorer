from pathlib import Path

from fastapi.testclient import TestClient

from explorer.api import create_app
from explorer.config import Settings
from explorer.db import Database
from explorer.indexer import Indexer
from explorer.queries import Queries
from explorer.rpc import XCoinRPC


def test_health_and_home(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    rpc = XCoinRPC(Settings())
    indexer = Indexer(db, rpc)
    app = create_app(Queries(db), indexer, rpc)
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["coin"] == "XFER"
    assert r.json()["version"] == "1.3.2"
    status = client.get("/api/status").json()
    assert "indexed_height" in status
    assert status["block_time_seconds"] == 60
    home = client.get("/")
    assert home.status_code == 200
    assert "XFER Explorer" in home.text
    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    js = client.get("/static/js/app.js")
    assert js.status_code == 200
    text = js.text
    assert "producer" not in text
    assert "txIdentityHtml" in text
    assert "winner_handle" in text
    assert "no XVA1" in text
    assert "linkAddr(w." not in text
    assert "loadAssetMedia" in text
    assert "xfer.mypinata.cloud" in text
    assert "pinataViewUrl" in text
    assert "ipfsContentUrl" in text
    assert "assetListIpfsCell" in text
    assert "PINATA_JWT" not in text
    assert text.index("add(pinataViewUrl(cid));") < text.index("add(ipfsContentUrl(cid));")
    cell = text[text.index("function assetListIpfsCell") : text.index("function mediaSrc")]
    assert "#/asset/" in cell
    assert "ipfsSources(cid)" in cell
    assert "fallbackAttr" in cell
    assert "target=\"_blank\"" not in cell
    assert "pinataViewUrl" not in cell
    assert "?v=explorer-polish-1" in home.text
    assert "formatAssetAmount" in text
    assert "pageAsset" in text
    assert "pageMembers" in text
    assert "pageStats" in text
    assert "Observatory" in text
    assert "guest share" in text
    assert "Share lottery wins" in text
    assert "active_nodes" in text
    assert "active_count" in text
    assert "producer" not in text
    assert "8000" not in text
    assert "setInterval(() => {\n  refreshStatus();" not in text
    assert "pollTipAndMaybeReload" in text
    assert "scheduleLiveRefresh" in text
    assert "DEFAULT_BLOCK_TIME_SECONDS" in text
    assert "/tip" in text
    tip = client.get("/api/tip")
    assert tip.status_code == 200
    tip_body = tip.json()
    assert "hash" in tip_body
    assert "height" in tip_body
    assert tip_body["block_time_seconds"] == 60
    assert tip_body["slot_seconds"] == 60
    stats = client.get("/api/stats")
    assert stats.status_code == 200
    assert "handles" in stats.json()
    assert "hat_pulse" in stats.json()
    assert "Eligible members" in text
    members = client.get("/api/lottery/members")
    assert members.status_code == 200
    assert "items" in members.json()
    root = Path(__file__).resolve().parent.parent
    setup_win = (root / "SETUP.bat").read_text(encoding="utf-8")
    setup_ps = (root / "scripts" / "setup-windows.ps1").read_text(encoding="utf-8")
    setup_sh = (root / "setup.sh").read_text(encoding="utf-8")
    for blob in (setup_win, setup_ps, setup_sh):
        assert "XferExplore" not in blob
        assert "rpcpassword=" not in blob.lower()
        assert ".pem" not in blob or "never" in blob.lower()
        assert "xattestor" not in blob.lower()
        assert "xlookupbearer" not in blob.lower()
        assert "rpcuser=" not in blob.lower()
    for blob in (setup_ps, setup_sh):
        assert "NiftyRaven/x-coin" in blob
        assert "server=1" in blob
        assert "releases?per_page=" in blob
        assert "Found installed wallet" in blob
        assert "No wallet found" in blob
        assert "wallet_release" in blob or "1.0.16-light" in blob or r"(\d+(?:\.\d+){1,3})" in blob
    db.close()


def test_public_tree_is_view_only_no_pinata_secrets():
    """This repo is public. Pinata JWT / pin APIs belong on x-coin-web, not here."""
    root = Path(__file__).resolve().parent.parent
    forbidden = (
        "PINATA_JWT",
        "PINATA_API_KEY",
        "PINATA_SECRET",
        "api.pinata.cloud/pinning",
        "/api/ipfs/pin",
    )
    suffixes = {".py", ".js", ".css", ".html", ".toml", ".md", ".txt", ".example"}
    for rel in ("explorer", "web", "config", "docs"):
        base = root / rel
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in text, f"{path.relative_to(root)} contains {token}"
    api = (root / "explorer" / "api.py").read_text(encoding="utf-8")
    assert "@app.post" not in api
    assert "@app.put" not in api
    assert "@app.patch" not in api
    js = (root / "web" / "js" / "app.js").read_text(encoding="utf-8")
    py = (root / "explorer" / "ipfs.py").read_text(encoding="utf-8")
    assert 'PINATA_VIEW_GATEWAY = "xfer.mypinata.cloud"' in js
    assert 'DEDICATED_GATEWAY = "https://xfer.mypinata.cloud/ipfs/"' in py
    assert py.index("DEDICATED_GATEWAY") < py.index("https://gateway.pinata.cloud/ipfs/")
    assert "ipfs_content_url" in py
    assert "@app.get(\"/api/ipfs/inspect\")" in api
    assert "@app.get(\"/api/ipfs/content/{cid}\")" in api
    assert "ERR_ID:00006" in py


class _FakeWallet:
    connected = True

    def __init__(self, info):
        self.info = info

    def try_call(self, method, *params, default=None):
        if method == "getlotteryinfo":
            return self.info
        if method == "getactivenodes":
            return []
        return default


def test_lottery_uses_wallet_active_nodes_and_falls_back(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    live_rpc = _FakeWallet(
        {"active_nodes": 7, "stamped_handles": ["alice", "bob"], "winners": []}
    )
    indexer = Indexer(db, live_rpc)
    client = TestClient(create_app(Queries(db), indexer, live_rpc))
    payload = client.get("/api/lottery").json()
    assert payload["active_count"] == 7
    assert payload["active_handles"] == ["alice", "bob"]

    old_rpc = _FakeWallet({"stamped_handles": ["alice", "bob"], "winners": []})
    old_client = TestClient(create_app(Queries(db), Indexer(db, old_rpc), old_rpc))
    old_payload = old_client.get("/api/lottery").json()
    assert old_payload["active_count"] == 2
    db.close()
