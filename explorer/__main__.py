"""python -m explorer"""

from __future__ import annotations

import argparse
import webbrowser

import uvicorn

from explorer.api import create_app
from explorer.config import load_settings
from explorer.db import Database
from explorer.indexer import Indexer
from explorer.queries import Queries
from explorer.rpc import XCoinRPC


def main() -> None:
    parser = argparse.ArgumentParser(description="X Coin blockchain & asset explorer")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    host = args.host or settings.bind_host
    port = args.port or settings.bind_port

    db = Database(settings.database)
    rpc = XCoinRPC(settings)
    rpc.connect()
    indexer = Indexer(db, rpc, settings.batch_size, settings.poll_seconds)
    indexer.start()
    queries = Queries(db)
    app = create_app(queries, indexer, rpc)

    url = f"http://{host}:{port}"
    print(f"X Coin explorer → {url}")
    if rpc.connected:
        print(f"Node RPC ok on port {rpc.rpc_port} ({rpc.network})")
    else:
        print("Waiting for a local X Coin wallet/node (RPC).")
        print("  Add server=1 to xcoin.conf, fully quit the wallet, start it once.")
        print("  Guide: docs/SETUP.md")
        print(f"  last error: {rpc.last_error}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        indexer.stop()
        rpc.close()
        db.close()


if __name__ == "__main__":
    main()
