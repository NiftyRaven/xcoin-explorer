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

from explorer.chain import COIN
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


def human_atoms(atoms: int) -> str:
    """Readable amount. At least one whole unit rounds to two decimals.

    Smaller amounts keep every significant place, up to 8. 54.23350448 XFER
    reads as 54.23. A fee under 1 XFER stays exact.
    """
    atoms = abs(int(atoms or 0))
    if atoms // COIN >= 1:
        step = COIN // 100
        rounded = ((atoms + step // 2) // step) * step
        whole = rounded // COIN
        cents = (rounded % COIN) // step
        if cents == 0:
            return f"{whole:,}"
        text = f"{cents:02d}".rstrip("0")
        return f"{whole:,}.{text}"
    frac = atoms % COIN
    if frac == 0:
        return f"{atoms // COIN:,}"
    return f"{atoms // COIN:,}.{frac:08d}".rstrip("0")


def _symbol(name: str) -> str:
    """Child ticker for ``LAUNCH_XFER/DOGX``. Other names stay whole."""
    text = str(name or "").strip()
    if "/" in text:
        child = text.split("/", 1)[1].strip()
        return child or text
    return text


def _txid(value: Any) -> str:
    return str(value or "").strip().lower()


def launch_fill_view(market: dict, fill: dict) -> dict | None:
    """One public Launch fill, shaped for the transaction page.

    ``txid`` is the user's payment. ``deliveryTxid`` is the market wallet's
    asset move, when the relay has recorded it. Refunds are not trades.
    """
    if str(fill.get("source") or "") == "refund":
        return None
    name = str(market.get("name") or "").strip()
    pay = _txid(fill.get("txid"))
    if not name or not pay:
        return None
    side = "sell" if fill.get("side") == "sell" else "buy"
    unique = "#" in name
    units = 0 if unique else _int(market.get("units"))
    asset_atoms = _asset_atoms(fill.get("tokens"), units, unique)
    xfer = _int(fill.get("xferons"))
    if asset_atoms <= 0 and xfer <= 0:
        return None
    market_fee = _int(fill.get("feeXferons"))
    creator_fee = _int(fill.get("creatorFeeXferons"))
    delivery = _txid(fill.get("deliveryTxid")) or None
    venue = "book" if fill.get("source") == "book" or fill.get("maker") else "launch"
    delivering = side == "buy" and venue == "launch" and not delivery and not fill.get("saleToProceeds")
    symbol = _symbol(name)
    verb = "Bought" if side == "buy" else "Sold"
    price = price_per_unit_atoms(xfer, asset_atoms)
    return {
        "side": side,
        "venue": venue,
        "asset": name,
        "symbol": symbol,
        "asset_atoms": asset_atoms,
        "xfer_atoms": xfer,
        "price_atoms": price,
        "market_fee_atoms": market_fee,
        "creator_fee_atoms": creator_fee,
        "payment_txid": pay,
        "delivery_txid": delivery,
        "delivering": delivering,
        "headline": f"Launch trade: {verb} {human_atoms(asset_atoms)} {symbol} for {human_atoms(xfer)} XFER",
        "amount": f"{human_atoms(asset_atoms)} {symbol}",
        "price": f"{human_atoms(price)} XFER",
        "market_fee": f"{human_atoms(market_fee)} XFER",
        "creator_fee": f"{human_atoms(creator_fee)} XFER",
    }


def index_launch_fills(markets: list[dict]) -> dict[str, dict]:
    """Map a payment txid and, when present, its delivery txid to one fill.

    A payment wins if the same id is also some other fill's delivery.
    """
    payments: dict[str, dict] = {}
    deliveries: dict[str, dict] = {}
    for market in markets or []:
        if not isinstance(market, dict):
            continue
        for fill in market.get("fills") or []:
            if not isinstance(fill, dict):
                continue
            view = launch_fill_view(market, fill)
            if not view:
                continue
            pay = view["payment_txid"]
            if pay not in payments:
                payments[pay] = {**view, "role": "payment"}
            delivery = view.get("delivery_txid") or ""
            if delivery and delivery != pay and delivery not in deliveries:
                deliveries[delivery] = {
                    **view,
                    "role": "delivery",
                    "delivering": False,
                    "headline": "Asset transfer for Launch trade",
                }
    indexed = dict(deliveries)
    indexed.update(payments)
    return indexed


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
        self._fills: dict[str, dict] = {}
        self.ok = False

    def rows(self) -> list[dict]:
        if not self.relay_url:
            return []
        now = time.monotonic()
        with self._lock:
            if self._at and now - self._at < self.cache_seconds:
                return list(self._rows)
            rows: list[dict] | None = None
            markets: list | None = None
            try:
                data = self.fetch(self.relay_url + "/api/launch/markets") or {}
                markets = data.get("markets") or []
                rows = trades_from_markets(markets)
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
            if markets is not None:
                self._fills = index_launch_fills(markets)
            self._at = now
            return list(self._rows)

    def launch_fill(self, txid: str, network_fee_atoms: int | None = None) -> dict | None:
        """Fill for a payment txid or the market wallet's delivery txid.

        Network fee is the chain fee on the payment. It is not part of the
        relay record, so the caller passes the indexed fee when it has one.
        A relay miss returns None and leaves the transaction page unchanged.
        """
        self.rows()
        key = _txid(txid)
        if not key:
            return None
        with self._lock:
            found = self._fills.get(key)
            if not found:
                return None
            out = dict(found)
        if out.get("role") != "payment":
            return out
        if network_fee_atoms is None:
            out["network_fee_atoms"] = None
            out["network_fee"] = None
            return out
        fee = int(network_fee_atoms)
        out["network_fee_atoms"] = fee
        out["network_fee"] = f"{human_atoms(fee)} XFER"
        return out


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
