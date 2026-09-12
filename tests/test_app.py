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
    status = client.get("/api/status").json()
    assert "indexed_height" in status
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
    db.close()
