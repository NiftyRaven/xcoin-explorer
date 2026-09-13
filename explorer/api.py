"""HTTP API + static explorer UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from explorer import __version__
from explorer.chain import GENESIS_HASH_MAIN, GENESIS_TIME_MAIN, NAME, SLOT_SECONDS, SUBUNIT, TICKER
from explorer.ipfs import attach_ipfs_fields, fetch_content, inspect_cid, valid_cid
from explorer.queries import Queries

WEB = Path(__file__).resolve().parent.parent / "web"


def create_app(queries: Queries, indexer, rpc) -> FastAPI:
    app = FastAPI(title="X Coin Explorer", version=__version__)
    app.state.queries = queries
    app.state.indexer = indexer
    app.state.rpc = rpc

    if WEB.exists():
        app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "coin": TICKER}

    @app.get("/api/status")
    def status():
        rpc_ok = bool(rpc.connected)
        extra = {
            "ticker": TICKER,
            "subunit": SUBUNIT,
            "name": NAME,
            "slot_seconds": SLOT_SECONDS,
            "genesis_hash_main": GENESIS_HASH_MAIN,
            "genesis_time_main": GENESIS_TIME_MAIN,
            "rpc_port": rpc.rpc_port,
            "live_nodes": indexer.live_nodes(),
        }
        try:
            if rpc.connected:
                extra["lottery"] = rpc.try_call("getlotteryinfo", default=None)
                extra["chain"] = rpc.try_call("getblockchaininfo", default=None)
                extra["net"] = rpc.try_call("getnetworkinfo", default=None)
                extra["peer_count"] = len(rpc.try_call("getpeerinfo", default=[]) or [])
                extra["mempool"] = rpc.try_call("getmempoolinfo", default=None)
        except Exception as e:
            extra["live_error"] = str(e)
        return queries.status(indexer.status, rpc_ok, rpc.last_error, extra)

    @app.get("/api/search")
    def search(q: str = ""):
        return queries.search(q)

    @app.get("/api/blocks")
    def blocks(limit: int = 25, before: int | None = None):
        return {"items": queries.recent_blocks(limit, before)}

    @app.get("/api/block/{key}")
    def block(key: str):
        b = queries.block(key)
        if not b:
            return JSONResponse({"error": "block not found"}, status_code=404)
        return b

    @app.get("/api/tx/{txid}")
    def tx(txid: str):
        t = queries.tx(txid)
        if not t and rpc.connected:
            raw = rpc.try_call("getrawtransaction", txid, True, default=None)
            if raw:
                return {"unindexed": True, "rpc": raw}
        if not t:
            return JSONResponse({"error": "transaction not found"}, status_code=404)
        return t

    @app.get("/api/address/{addr}")
    def address(addr: str, limit: int = 50):
        return queries.address(addr, limit)

    @app.get("/api/assets")
    def assets(q: str = "", kind: str = "", limit: int = 50, offset: int = 0):
        data = queries.assets(q, kind, limit, offset)
        if rpc.connected:
            for item in data.get("items") or []:
                if item.get("ipfs_cid") or item.get("ipfs"):
                    continue
                rpc_data = rpc.try_call("getassetdata", item.get("name"), default=None)
                if isinstance(rpc_data, dict):
                    attach_ipfs_fields(item, rpc_data)
                    if item.get("ipfs_cid"):
                        queries.store_ipfs(item["name"], item["ipfs_cid"])
        return data

    @app.get("/api/asset/{name:path}")
    def asset(name: str, limit: int = 50):
        a = queries.asset(name, limit)
        if not a:
            return JSONResponse({"error": "asset not found"}, status_code=404)
        if rpc.connected:
            a["rpc"] = rpc.try_call("getassetdata", name, default=None)
        attach_ipfs_fields(a, a.get("rpc") if isinstance(a.get("rpc"), dict) else None)
        if a.get("ipfs_cid"):
            queries.store_ipfs(a["name"], a["ipfs_cid"])
        return a

    @app.get("/api/ipfs/inspect")
    def ipfs_inspect(cid: str = ""):
        headers = {"Cache-Control": "no-store"}
        try:
            return JSONResponse(inspect_cid(cid), headers=headers)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400, headers=headers)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502, headers=headers)

    @app.get("/api/ipfs/content/{cid}")
    def ipfs_content(cid: str):
        if not valid_cid(cid):
            return JSONResponse({"error": "invalid IPFS CID"}, status_code=400)
        try:
            body, content_type = fetch_content(cid)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        return Response(
            content=body,
            media_type=content_type or "application/octet-stream",
            headers={
                "Cache-Control": "public, max-age=86400",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/lottery")
    def lottery():
        live = None
        last_xva = queries.last_xva()
        id_map: dict[str, str] = {}
        for row in last_xva:
            nid = (row.get("node_id") or "").lower()
            handle = (row.get("xaccount") or "").lower().lstrip("@")
            if nid and handle:
                id_map[nid] = handle
        # getactivenodes is not the seed hat (player nodes often omit themselves).
        # Use it only as a supplemental id → handle map.
        extra = []
        if rpc.connected:
            live = rpc.try_call("getlotteryinfo", default=None)
            extra = rpc.try_call("getactivenodes", default=None) or indexer.live_nodes() or []
        for n in extra:
            nid = (n.get("id") or "").lower()
            handle = (n.get("xaccount") or "").lower().lstrip("@")
            if nid and handle:
                id_map[nid] = handle
        stamped = None
        if isinstance(live, dict):
            stamped = live.get("stamped_handles")
        if stamped:
            active_handles = [str(h).lower().lstrip("@") for h in stamped if h]
        else:
            active_handles = []
            seen: set[str] = set()
            for row in last_xva:
                h = (row.get("xaccount") or "").lower().lstrip("@")
                if h and h not in seen:
                    seen.add(h)
                    active_handles.append(h)
        winner_handles: list[str] = []
        if isinstance(live, dict):
            for wid in live.get("winners") or []:
                h = id_map.get(str(wid).lower())
                if h:
                    winner_handles.append(h)
        nodes = [{"xaccount": h} for h in active_handles]
        return {
            "live": live,
            "nodes": nodes,
            "active_handles": active_handles,
            "winner_handles": winner_handles,
            "rpc_connected": bool(rpc.connected),
            "history": queries.lottery_history(30),
            "leaders": queries.lottery_leaders(40),
        }

    @app.get("/api/lottery/winners")
    def lottery_winners(limit: int = 40, before: int | None = None):
        return {"items": queries.lottery_history(limit, before)}

    @app.get("/api/lottery/leaders")
    def lottery_leaders(limit: int = 50):
        return {"items": queries.lottery_leaders(limit)}

    @app.get("/api/rich")
    def rich(limit: int = 50):
        return {"items": queries.rich_list(limit)}

    @app.get("/api/mempool")
    def mempool():
        if not rpc.connected:
            return {"connected": False, "txs": [], "info": None}
        info = rpc.try_call("getmempoolinfo", default={})
        ids = rpc.try_call("getrawmempool", default=[]) or []
        txs = []
        for txid in ids[:50]:
            raw = rpc.try_call("getrawtransaction", txid, True, default=None)
            if raw:
                txs.append(
                    {
                        "txid": txid,
                        "size": raw.get("size"),
                        "vin": len(raw.get("vin") or []),
                        "vout": len(raw.get("vout") or []),
                    }
                )
            else:
                txs.append({"txid": txid})
        return {"connected": True, "info": info, "txs": txs, "count": len(ids)}

    @app.get("/api/peers")
    def peers():
        if not rpc.connected:
            return {"connected": False, "peers": []}
        peers = rpc.try_call("getpeerinfo", default=[]) or []
        slim = []
        for p in peers:
            slim.append(
                {
                    "addr": p.get("addr"),
                    "subver": p.get("subver"),
                    "synced_blocks": p.get("synced_blocks"),
                    "startingheight": p.get("startingheight"),
                    "inbound": p.get("inbound"),
                    "conntime": p.get("conntime"),
                    "pingtime": p.get("pingtime"),
                }
            )
        return {"connected": True, "peers": slim}

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # hash-router SPA; unknown paths still get the app
        if full_path.startswith("api/"):
            return JSONResponse({"error": "not found"}, status_code=404)
        target = WEB / full_path
        if target.exists() and target.is_file():
            return FileResponse(target)
        return FileResponse(WEB / "index.html")

    return app
