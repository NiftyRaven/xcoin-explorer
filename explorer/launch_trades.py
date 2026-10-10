"""All-time Launch trades from the Launch relay's public data.

The chain scan in ``trades.py`` only recognises payments to a short list of
proceeds addresses and only looks at today. Launch now settles through
per-listing reserves, order-book escrows and auction escrows, so most trades
never matched. The relay already publishes every fill it settled
(``/api/launch/markets`` -> ``fills``) and every settled auction
(``/api/auctions``). This module reads those public, read-only endpoints,
caches them briefly in memory and turns them into explorer trade rows.

Nothing here writes to sqlite. The DB is only read to name the real trader
when the relay records an escrow address for an order-book fill.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable

from explorer.trades import (
    DEFAULT_LAUNCH_PROCEEDS,
    LAUNCH_TREASURY,
    describe_trade,
    price_per_unit_atoms,
    short_address,
)

CACHE_SECONDS = 20.0
MAX_BYTES = 8_000_000


def _http_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "xcoin-explorer"})
    with urllib.request.urlopen(req, timeout=5) as res:  # noqa: S310 - configured relay URL
        return json.loads(res.read(MAX_BYTES).decode("utf-8"))


def _int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _secs(ms: Any) -> int | None:
    n = _int(ms)
    if n <= 0:
        return None
    return n // 1000 if n > 10_000_000_000 else n


def _asset_atoms(tokens: Any, units: int, unique: bool) -> int:
    """Relay ``tokens`` are raw units; explorer amounts are 8-decimal atoms."""
    raw = _int(tokens)
    if unique:
        return raw * 100_000_000
    return raw * (10 ** max(0, 8 - int(units or 0)))


def trades_from_markets(markets: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in markets or []:
        name = str(m.get("name") or "")
        if not name:
            continue
        unique = "#" in name
        units = 0 if unique else _int(m.get("units"))
        reserve = str(m.get("reserveAddress") or "")
        for f in m.get("fills") or []:
            txid = str(f.get("txid") or "")
            side = "sell" if f.get("side") == "sell" else "buy"
            if not txid:
                continue
            asset_atoms = _asset_atoms(f.get("tokens"), units, unique)
            xfer = _int(f.get("xferons"))
            venue = "book" if f.get("source") == "book" or f.get("maker") else "launch"
            out.append(
                {
                    "side": side,
                    "asset": name,
                    "asset_type": "unique" if unique else ("sub" if "/" in name else "root"),
                    "asset_atoms": asset_atoms,
                    "xfer_atoms": xfer,
                    "fee_atoms": _int(f.get("feeXferons")),
                    "price_atoms": price_per_unit_atoms(xfer, asset_atoms),
                    "trader": str(f.get("address") or ""),
                    "counterparty": str(f.get("maker") or reserve or ""),
                    "reserve": reserve,
                    "tokens_pending": side == "buy" and venue == "launch" and not f.get("deliveryTxid") and not f.get("saleToProceeds"),
                    "delivery_txid": f.get("deliveryTxid") or None,
                    "venue": venue,
                    "time": _secs(f.get("time")),
                    "txid": txid,
                    "source": "relay",
                }
            )
    return out


def trades_from_auctions(auctions: list[dict]) -> list[dict]:
    out: list[dict] = []
    for a in auctions or []:
        txid = str(a.get("settleTxid") or "")
        winner = str(a.get("winner") or "")
        if not txid or not winner:
            continue
        name = str(a.get("name") or "")
        unique = "#" in name
        asset_atoms = _asset_atoms(a.get("qtyRaw") or 1, 0 if unique else _int(a.get("units")), unique)
        xfer = _int(a.get("soldXferons"))
        out.append(
            {
                "side": "buy",
                "asset": name,
                "asset_type": "unique" if unique else ("sub" if "/" in name else "root"),
                "asset_atoms": asset_atoms,
                "xfer_atoms": xfer,
                "fee_atoms": 0,
                "price_atoms": price_per_unit_atoms(xfer, asset_atoms),
                "trader": winner,
                "counterparty": str(a.get("seller") or ""),
                "reserve": "",
                "tokens_pending": False,
                "delivery_txid": None,
                "venue": "auction",
                "time": _secs(a.get("endAt")),
                "txid": txid,
                "source": "relay",
            }
        )
    return out


class RelayTrades:
    """Short-lived in-memory copy of every relay trade. Stale copy is kept on errors."""

    def __init__(self, relay_url: str = "", *, fetch: Callable[[str], Any] = _http_json, cache_seconds: float = CACHE_SECONDS):
        self.relay_url = (relay_url or "").rstrip("/")
        self.fetch = fetch
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._at = 0.0
        self._rows: list[dict] = []
        self.ok = False

    def rows(self) -> list[dict]:
        if not self.relay_url:
            return []
        now = time.monotonic()
        with self._lock:
            if self._at and now - self._at < self.cache_seconds:
                return list(self._rows)
            rows: list[dict] | None = None
            try:
                data = self.fetch(self.relay_url + "/api/launch/markets") or {}
                rows = trades_from_markets(data.get("markets") or [])
                self.ok = True
            except Exception:
                self.ok = False
            try:
                data = self.fetch(self.relay_url + "/api/auctions") or {}
                if rows is not None:
                    rows += trades_from_auctions(data.get("auctions") or [])
            except Exception:
                pass
            if rows is not None:
                self._rows = rows
            self._at = now
            return list(self._rows)


def platform_addresses(proceeds: Any = None) -> set[str]:
    return set(proceeds or DEFAULT_LAUNCH_PROCEEDS) | set(DEFAULT_LAUNCH_PROCEEDS) | {LAUNCH_TREASURY}


def resolve_trader(db, trade: dict, platform: set[str]) -> str:
    """Real trader for a fill the relay booked to an escrow address.

    Buy: the largest non-platform XFER input. Sell: whoever put the asset in.
    """
    trader = trade.get("trader") or ""
    if trader and trader not in platform:
        return trader
    try:
        rows = db.conn.execute(
            "SELECT address, value, asset FROM txio WHERE txid=? AND direction='in'",
            (trade.get("txid") or "",),
        ).fetchall()
    except Exception:
        return trader
    best, best_v = "", -1
    for r in rows:
        addr = r["address"]
        if not addr or addr in platform:
            continue
        if trade.get("side") == "sell":
            if r["asset"] and r["asset"] == trade.get("asset"):
                return addr
            continue
        if not r["asset"] and int(r["value"] or 0) > best_v:
            best, best_v = addr, int(r["value"] or 0)
    return best or trader


def finish(trade: dict, handle: str) -> dict:
    who = f"@{handle}" if handle else short_address(trade.get("trader"))
    trade["trader_handle"] = handle or None
    trade["sentence"] = describe_trade(trade["side"], who, trade["asset_atoms"], trade["asset"], trade["xfer_atoms"])
    return trade
