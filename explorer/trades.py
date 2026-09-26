"""Launch buy/sell detection for the public Trades page.

Launch (the bonding-curve market) is not in this repo. There is no fills
client here, so a trade is recognized from the chain:

* one configured proceeds address is a party (buys pay it, sells are paid by it)
* that address and one other address swap XFER for a single asset
* change back to the sender is netted out and is not counted as a payment
* the network fee is reported on its own

The known proceeds address is the default. ``explorer.toml`` ``[launch]``
or ``XFER_LAUNCH_PROCEEDS`` replaces that list.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from explorer.amounts import format_xfer, xfer_to_atoms
from explorer.chain import COIN, classify_asset_name
from explorer.queries import norm_handle, paginate

# Receives XFER on a Launch buy and pays XFER on a Launch sell.
DEFAULT_LAUNCH_PROCEEDS = ("XvmKQ4Rf1PtETDRMaaCeVgtKmGqqieYfQF",)

# How often to re-read the mempool. Confirmed trades come from the index.
MEMPOOL_CACHE_SECONDS = 8.0
# Stop a single page scan so one request cannot walk the whole chain.
MAX_CANDIDATES_PER_PAGE = 2000
CANDIDATE_BATCH = 40


def parse_proceeds(value: Any, *, fallback: bool = True) -> tuple[str, ...]:
    """Normalize a proceeds address list. Blank entries are dropped."""
    if value is None:
        return DEFAULT_LAUNCH_PROCEEDS if fallback else ()
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            parts.extend(str(item).split(","))
    else:
        parts = [str(value)]
    out: list[str] = []
    for part in parts:
        addr = str(part).strip()
        if addr and addr not in out:
            out.append(addr)
    if not out and fallback:
        return DEFAULT_LAUNCH_PROCEEDS
    return tuple(out)


def _atoms(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return xfer_to_atoms(value)


def format_asset_amount(atoms: int, name: str = "") -> str:
    """Format an asset qty stored in 1e8 units. Trailing zeros are dropped."""
    atoms = abs(int(atoms or 0))
    whole = atoms // COIN
    frac = atoms % COIN
    if frac == 0:
        body = f"{whole:,}"
    else:
        frac_s = f"{frac:08d}".rstrip("0")
        body = f"{whole:,}.{frac_s}"
    if name:
        return f"{body} {name}"
    return body


def short_address(addr: str | None) -> str:
    if not addr:
        return ""
    if len(addr) <= 12:
        return addr
    return f"{addr[:6]}…{addr[-4:]}"


def price_per_unit_atoms(xfer_atoms: int, asset_atoms: int) -> int:
    """XFER atoms for one whole asset unit (1e8 asset units)."""
    if asset_atoms <= 0 or xfer_atoms <= 0:
        return 0
    return int(xfer_atoms) * COIN // int(asset_atoms)


def describe_trade(
    side: str,
    who: str,
    asset_atoms: int,
    asset: str,
    xfer_atoms: int,
) -> str:
    verb = "bought" if side == "buy" else "sold"
    return f"{who} {verb} {format_asset_amount(asset_atoms, asset)} for {format_xfer(xfer_atoms)}"


def _add(bucket: dict[str, int], addr: str | None, amount: int) -> None:
    if not addr or not amount:
        return
    bucket[addr] = bucket.get(addr, 0) + int(amount)


def _nets(rows_in: dict[str, int], rows_out: dict[str, int]) -> dict[str, int]:
    """Output minus input. Change back to the same address cancels."""
    nets: dict[str, int] = {}
    for addr in set(rows_in) | set(rows_out):
        net = rows_out.get(addr, 0) - rows_in.get(addr, 0)
        if net:
            nets[addr] = net
    return nets


def classify_launch_trade(
    tx: dict,
    proceeds: Any = None,
) -> dict | None:
    """Return one Launch trade, or None if this tx is not a buy or sell.

    ``tx`` uses the index shape: ``vin`` / ``vout`` rows with ``address``,
    ``value`` (XFER atoms), ``asset``, ``asset_amount``, and ``asset_kind``.
    ``height`` may be None for a mempool tx. Amounts are chain atoms only.
    """
    if not tx or tx.get("coinbase"):
        return None
    if proceeds is None:
        allowed = set(DEFAULT_LAUNCH_PROCEEDS)
    else:
        allowed = set(parse_proceeds(proceeds, fallback=False))
    if not allowed:
        return None

    vin = list(tx.get("vin") or [])
    vout = list(tx.get("vout") or [])
    xfer_in: dict[str, int] = {}
    xfer_out: dict[str, int] = {}
    asset_in: dict[str, dict[str, int]] = {}
    asset_out: dict[str, dict[str, int]] = {}
    bad_kinds: set[str] = set()

    def note_asset(side: dict[str, dict[str, int]], addr: str | None, row: dict) -> None:
        name = row.get("asset") or ""
        amount = int(row.get("asset_amount") or 0)
        if not addr or not name or amount <= 0:
            return
        if name.endswith("!"):
            return
        kind = (row.get("asset_kind") or "").lower()
        if kind in ("new", "reissue", "owner"):
            bad_kinds.add(name)
            return
        side.setdefault(addr, {})
        side[addr][name] = side[addr].get(name, 0) + amount

    for row in vin:
        addr = row.get("address")
        _add(xfer_in, addr, _atoms(row.get("value")))
        note_asset(asset_in, addr, row)
    for row in vout:
        addr = row.get("address")
        _add(xfer_out, addr, _atoms(row.get("value")))
        note_asset(asset_out, addr, row)

    fee = sum(xfer_in.values()) - sum(xfer_out.values())
    if fee < 0:
        fee = 0

    xfer_net = _nets(xfer_in, xfer_out)
    names: set[str] = set()
    asset_net: dict[str, dict[str, int]] = {}
    for addr in set(asset_in) | set(asset_out):
        nets = _nets(asset_in.get(addr) or {}, asset_out.get(addr) or {})
        if nets:
            asset_net[addr] = nets
            names.update(nets)

    present = (set(xfer_net) | set(asset_net)) & allowed
    if len(present) != 1:
        return None
    counterparty = next(iter(present))

    found: list[dict] = []
    for name in sorted(names):
        if name in bad_kinds:
            continue
        p_asset = (asset_net.get(counterparty) or {}).get(name, 0)
        if not p_asset:
            continue
        traders = []
        for addr, nets in asset_net.items():
            if addr == counterparty or addr in allowed:
                continue
            t_asset = nets.get(name, 0)
            if t_asset and abs(t_asset) == abs(p_asset) and (t_asset > 0) != (p_asset > 0):
                traders.append((addr, t_asset))
        if len(traders) != 1:
            continue
        trader, t_asset = traders[0]
        # Nobody else keeps or loses this asset.
        if any(
            (nets.get(name) or 0)
            for addr, nets in asset_net.items()
            if addr not in (trader, counterparty)
        ):
            continue
        p_xfer = xfer_net.get(counterparty, 0)
        t_xfer = xfer_net.get(trader, 0)
        if any(addr not in (trader, counterparty) for addr in xfer_net):
            continue
        if t_asset > 0 and p_asset < 0 and p_xfer > 0 and t_xfer < 0:
            side = "buy"
            xfer_atoms = p_xfer
        elif t_asset < 0 and p_asset > 0 and p_xfer < 0 and t_xfer > 0:
            side = "sell"
            xfer_atoms = t_xfer
        else:
            continue
        # The XFER gap between the two sides is the fee, nothing else.
        if abs(abs(t_xfer) - abs(p_xfer)) != fee:
            continue
        asset_atoms = abs(t_asset)
        if xfer_atoms <= 0 or asset_atoms <= 0:
            continue
        found.append(
            {
                "side": side,
                "asset": name,
                "asset_type": classify_asset_name(name),
                "asset_atoms": asset_atoms,
                "xfer_atoms": xfer_atoms,
                "fee_atoms": fee,
                "price_atoms": price_per_unit_atoms(xfer_atoms, asset_atoms),
                "trader": trader,
                "counterparty": counterparty,
            }
        )

    if len(found) != 1:
        return None
    trade = found[0]
    who = short_address(trade["trader"])
    trade["sentence"] = describe_trade(
        trade["side"], who, trade["asset_atoms"], trade["asset"], trade["xfer_atoms"]
    )
    return trade


def _prevout(db, txid: str, n: int):
    row = db.conn.execute(
        """
        SELECT address, value, asset, asset_amount, asset_kind
        FROM txio WHERE txid=? AND n=? AND direction='out'
        """,
        (txid, n),
    ).fetchone()
    if row:
        return row
    return db.conn.execute(
        """
        SELECT address, value, asset, asset_amount, NULL AS asset_kind
        FROM utxos WHERE txid=? AND n=?
        """,
        (txid, n),
    ).fetchone()


def tx_view_from_rpc(raw: dict, db) -> dict | None:
    """Build a classifier tx from a verbose mempool transaction.

    Inputs are read from the index (the node is not asked for every parent).
    """
    if not isinstance(raw, dict):
        return None
    txid = raw.get("txid") or raw.get("hash")
    if not txid:
        return None
    vin = []
    for vin_row in raw.get("vin") or []:
        if "coinbase" in vin_row:
            return None
        prev = _prevout(db, vin_row.get("txid") or "", int(vin_row.get("vout") or 0))
        if not prev:
            vin.append(
                {
                    "address": None,
                    "value": 0,
                    "asset": None,
                    "asset_amount": 0,
                    "asset_kind": None,
                }
            )
            continue
        vin.append(
            {
                "address": prev["address"],
                "value": int(prev["value"] or 0),
                "asset": prev["asset"],
                "asset_amount": int(prev["asset_amount"] or 0),
                "asset_kind": prev["asset_kind"],
            }
        )
    vout = []
    for i, vout_row in enumerate(raw.get("vout") or []):
        spk = vout_row.get("scriptPubKey") or {}
        addresses = spk.get("addresses") or []
        address = addresses[0] if addresses else None
        asset = spk.get("asset") if isinstance(spk.get("asset"), dict) else None
        name = asset.get("name") if asset else None
        amount = _atoms(asset.get("amount")) if asset else 0
        kind = {
            "new_asset": "new",
            "reissue_asset": "reissue",
            "transfer_asset": "transfer",
        }.get(spk.get("type") or "", "transfer" if name else None)
        vout.append(
            {
                "address": address,
                "value": _atoms(vout_row.get("value")),
                "asset": name,
                "asset_amount": amount,
                "asset_kind": kind,
                "n": int(vout_row.get("n") if vout_row.get("n") is not None else i),
            }
        )
    when = raw.get("time") or raw.get("blocktime")
    try:
        when_i = int(when) if when else None
    except (TypeError, ValueError):
        when_i = None
    return {
        "txid": txid,
        "height": None,
        "n": None,
        "time": when_i,
        "coinbase": False,
        "vin": vin,
        "vout": vout,
    }


class TradeFeed:
    """Confirmed trades from the index, mempool trades from a short-lived cache."""

    def __init__(self, db, rpc, indexer, proceeds: Any = None, *, cache_seconds: float = MEMPOOL_CACHE_SECONDS):
        self.db = db
        self.rpc = rpc
        self.indexer = indexer
        if proceeds is None:
            self.proceeds = DEFAULT_LAUNCH_PROCEEDS
        else:
            self.proceeds = parse_proceeds(proceeds, fallback=False)
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._mem: tuple[float, tuple, list[dict]] | None = None

    def tip_height(self) -> int:
        tip = None
        if self.indexer is not None:
            tip = (getattr(self.indexer, "status", None) or {}).get("tip")
        try:
            if tip is not None and int(tip) >= 0:
                return int(tip)
        except (TypeError, ValueError):
            pass
        return int(self.db.indexed_height())

    def _handles(self, addresses: list[str]) -> dict[str, str]:
        addrs = [a for a in addresses if a]
        if not addrs:
            return {}
        marks = ",".join("?" * len(addrs))
        rows = self.db.conn.execute(
            f"SELECT address, handle FROM identities WHERE address IN ({marks})",
            addrs,
        ).fetchall()
        out: dict[str, str] = {}
        for row in rows:
            addr = row["address"]
            handle = norm_handle(row["handle"])
            if addr and handle and addr not in out:
                out[addr] = handle
        return out

    def _handle_addresses(self, handle: str) -> set[str]:
        name = norm_handle(handle)
        if not name:
            return set()
        rows = self.db.conn.execute(
            "SELECT address FROM identities WHERE handle=?",
            (name,),
        ).fetchall()
        return {row["address"] for row in rows if row["address"]}

    def _decorate(self, base: dict, tx: dict, handles: dict[str, str] | None = None) -> dict:
        trader = base["trader"]
        handle = (handles or {}).get(trader) or ""
        who = f"@{handle}" if handle else short_address(trader)
        height = tx.get("height")
        try:
            height_i = int(height) if height is not None else None
        except (TypeError, ValueError):
            height_i = None
        tip = self.tip_height()
        if height_i is None:
            confirmations = 0
            confirmed = False
        else:
            confirmations = max(0, tip - height_i + 1) if tip >= 0 else 1
            confirmed = confirmations >= 1
        when = tx.get("time")
        try:
            when_i = int(when) if when else None
        except (TypeError, ValueError):
            when_i = None
        n = tx.get("n")
        try:
            n_i = int(n) if n is not None else None
        except (TypeError, ValueError):
            n_i = None
        record = dict(base)
        record.update(
            {
                "txid": tx.get("txid") or "",
                "height": height_i,
                "n": n_i,
                "time": when_i,
                "trader_handle": handle or None,
                "confirmations": confirmations,
                "confirmed": confirmed,
                "sentence": describe_trade(
                    base["side"], who, base["asset_atoms"], base["asset"], base["xfer_atoms"]
                ),
            }
        )
        return record

    def _proceeds_sql(self) -> tuple[str, list[str]]:
        marks = ",".join("?" * len(self.proceeds))
        return marks, list(self.proceeds)

    def _candidate_batch(self, before_height: int | None, before_n: int | None, limit: int) -> list[dict]:
        if not self.proceeds:
            return []
        marks, params = self._proceeds_sql()
        sql = f"""
            SELECT t.txid, t.height, t.n, t.time, t.fee, t.coinbase
            FROM txs t
            WHERE t.coinbase = 0
              AND EXISTS (
                SELECT 1 FROM txio i
                WHERE i.txid = t.txid AND i.address IN ({marks})
              )
        """
        args: list[Any] = list(params)
        if before_height is not None:
            n = 0 if before_n is None else int(before_n)
            sql += " AND (t.height < ? OR (t.height = ? AND t.n < ?))"
            args.extend([int(before_height), int(before_height), n])
        sql += " ORDER BY t.height DESC, t.n DESC LIMIT ?"
        args.append(int(limit))
        return [dict(row) for row in self.db.conn.execute(sql, args).fetchall()]

    def _load_views(self, metas: list[dict]) -> dict[str, dict]:
        if not metas:
            return {}
        ids = [m["txid"] for m in metas]
        marks = ",".join("?" * len(ids))
        rows = self.db.conn.execute(
            f"SELECT * FROM txio WHERE txid IN ({marks}) ORDER BY txid, direction, n",
            ids,
        ).fetchall()
        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row["txid"], []).append(row)
        views = {}
        for meta in metas:
            vin = []
            vout = []
            for row in grouped.get(meta["txid"], []):
                item = {
                    "address": row["address"],
                    "value": int(row["value"] or 0),
                    "asset": row["asset"],
                    "asset_amount": int(row["asset_amount"] or 0),
                    "asset_kind": row["asset_kind"],
                }
                if row["direction"] == "in":
                    vin.append(item)
                else:
                    vout.append(item)
            views[meta["txid"]] = {
                "txid": meta["txid"],
                "height": meta["height"],
                "n": meta["n"],
                "time": meta["time"],
                "coinbase": bool(meta["coinbase"]),
                "vin": vin,
                "vout": vout,
            }
        return views

    def _match(self, trade: dict, side: str, q: str, handle_addrs: set[str]) -> bool:
        if side in ("buy", "sell") and trade.get("side") != side:
            return False
        query = (q or "").strip()
        if not query:
            return True
        if query.lower() == str(trade.get("txid") or "").lower():
            return True
        if query in (trade.get("trader"), trade.get("counterparty")):
            return True
        if query.lower() in str(trade.get("asset") or "").lower():
            return True
        handle = norm_handle(query)
        trader_handle = norm_handle(trade.get("trader_handle"))
        if handle and trader_handle and handle == trader_handle:
            return True
        if handle_addrs and trade.get("trader") in handle_addrs:
            return True
        return False

    def mempool_trades(self, *, now: float | None = None) -> list[dict]:
        rpc = self.rpc
        if not self.proceeds or rpc is None or not getattr(rpc, "connected", False):
            return []
        clock = time.monotonic() if now is None else now
        ids = rpc.try_call("getrawmempool", default=[]) or []
        if not isinstance(ids, list):
            return []
        key = tuple(str(i) for i in ids)
        with self._lock:
            cached = self._mem
            if cached and cached[1] == key and clock - cached[0] < self.cache_seconds:
                return list(cached[2])
        trades: list[dict] = []
        # Cap the lookup so a busy mempool does not become one RPC per tx forever.
        for txid in key[:100]:
            raw = rpc.try_call("getrawtransaction", txid, True, default=None)
            view = tx_view_from_rpc(raw, self.db) if isinstance(raw, dict) else None
            if not view:
                continue
            if not view.get("time"):
                view["time"] = int(time.time())
            base = classify_launch_trade(view, self.proceeds)
            if not base:
                continue
            trades.append(self._decorate(base, view, self._handles([base["trader"]])))
        with self._lock:
            self._mem = (clock, key, trades)
        return trades

    def _one_txid(self, txid: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT txid, height, n, time, fee, coinbase FROM txs WHERE txid=?",
            (txid,),
        ).fetchone()
        if not row:
            row = self.db.conn.execute(
                "SELECT txid, height, n, time, fee, coinbase FROM txs WHERE txid=?",
                (txid.lower(),),
            ).fetchone()
        if not row or row["coinbase"]:
            return None
        meta = dict(row)
        view = self._load_views([meta]).get(meta["txid"])
        if not view:
            return None
        base = classify_launch_trade(view, self.proceeds)
        if not base:
            return None
        return self._decorate(base, view, self._handles([base["trader"]]))

    def confirmed_page(
        self,
        *,
        side: str,
        q: str,
        handle_addrs: set[str],
        before_height: int | None,
        before_n: int | None,
        limit: int,
    ) -> tuple[list[dict], bool, int | None, int | None]:
        items: list[dict] = []
        cursor_h = before_height
        cursor_n = before_n
        scanned = 0
        stopped_early = False
        while len(items) < limit and scanned < MAX_CANDIDATES_PER_PAGE:
            batch = self._candidate_batch(cursor_h, cursor_n, CANDIDATE_BATCH)
            if not batch:
                break
            scanned += len(batch)
            views = self._load_views(batch)
            classified = []
            for meta in batch:
                view = views.get(meta["txid"])
                if not view:
                    continue
                base = classify_launch_trade(view, self.proceeds)
                if base:
                    classified.append((meta, view, base))
            handles = self._handles([base["trader"] for _, _, base in classified]) if classified else {}
            for meta, view, base in classified:
                trade = self._decorate(base, view, handles)
                if self._match(trade, side, q, handle_addrs):
                    items.append(trade)
                    if len(items) >= limit:
                        stopped_early = True
                        break
            if stopped_early:
                break
            tail = batch[-1]
            cursor_h = int(tail["height"])
            cursor_n = int(tail["n"])
            if len(batch) < CANDIDATE_BATCH:
                break
        if not items:
            hit_cap = scanned >= MAX_CANDIDATES_PER_PAGE and cursor_h is not None
            return [], hit_cap, cursor_h, cursor_n
        last = items[-1]
        next_h = int(last["height"]) if last.get("height") is not None else cursor_h
        next_n = last.get("n") if last.get("n") is not None else cursor_n
        if stopped_early and next_h is not None and next_n is not None:
            has_more = bool(self._candidate_batch(int(next_h), int(next_n), 1))
        elif scanned >= MAX_CANDIDATES_PER_PAGE and cursor_h is not None:
            has_more = True
            next_h = cursor_h
            next_n = cursor_n
        else:
            has_more = False
        return items, has_more, next_h, next_n

    def stats(self, *, now: int | None = None, pending: list[dict] | None = None) -> dict:
        moment = int(time.time() if now is None else now)
        pending_rows = self.mempool_trades() if pending is None else pending
        hour = moment - 3600
        day = moment - 86400
        confirmed = self._trades_since(day)
        seen = {t["txid"] for t in confirmed}
        hour_n = 0
        volume = 0
        for trade in confirmed:
            when = trade.get("time") or 0
            if when >= hour:
                hour_n += 1
            if when >= day:
                volume += int(trade.get("xfer_atoms") or 0)
        for trade in pending_rows:
            if trade.get("txid") in seen:
                continue
            when = trade.get("time") or moment
            if when >= hour:
                hour_n += 1
            if when >= day:
                volume += int(trade.get("xfer_atoms") or 0)
        height = self.tip_height()
        return {
            "height": height if height >= 0 else None,
            "pending": len(pending_rows),
            "trades_last_hour": hour_n,
            "volume_24h_atoms": volume,
        }

    def _trades_since(self, since: int) -> list[dict]:
        if not self.proceeds:
            return []
        marks, params = self._proceeds_sql()
        rows = self.db.conn.execute(
            f"""
            SELECT t.txid, t.height, t.n, t.time, t.fee, t.coinbase
            FROM txs t
            WHERE t.coinbase = 0 AND t.time >= ?
              AND EXISTS (
                SELECT 1 FROM txio i
                WHERE i.txid = t.txid AND i.address IN ({marks})
              )
            ORDER BY t.height DESC, t.n DESC
            """,
            [since, *params],
        ).fetchall()
        metas = [dict(r) for r in rows]
        views = self._load_views(metas)
        found = []
        for meta in metas:
            view = views.get(meta["txid"])
            if not view:
                continue
            base = classify_launch_trade(view, self.proceeds)
            if base:
                found.append((view, base))
        handles = self._handles([base["trader"] for _, base in found])
        return [self._decorate(base, view, handles) for view, base in found]

    def page(
        self,
        *,
        side: str = "all",
        q: str = "",
        before_height: int | None = None,
        before_n: int | None = None,
        limit: int = 25,
        now: int | None = None,
    ) -> dict:
        limit = paginate(limit, 25, 50)
        side = side if side in ("buy", "sell") else "all"
        query = (q or "").strip()[:200]
        handle_addrs = self._handle_addresses(query) if query.startswith("@") or query else set()
        if query and not query.startswith("@") and not query[:1].isdigit():
            # A bare handle still resolves when an identity exists.
            extra = self._handle_addresses(query)
            if extra:
                handle_addrs |= extra
        pending = self.mempool_trades()
        items: list[dict] = []
        if before_height is None:
            for trade in pending:
                if self._match(trade, side, query, handle_addrs):
                    items.append(trade)
                if len(items) >= limit:
                    break
        txid_only = len(query) == 64 and all(c in "0123456789abcdefABCDEF" for c in query)
        confirmed: list[dict] = []
        has_more = False
        next_h = None
        next_n = None
        if txid_only:
            one = self._one_txid(query)
            if one and self._match(one, side, query, handle_addrs):
                if before_height is None and one["txid"] not in {t["txid"] for t in items}:
                    confirmed = [one]
        else:
            room = max(0, limit - len(items)) if before_height is None else limit
            if room > 0:
                confirmed, has_more, next_h, next_n = self.confirmed_page(
                    side=side,
                    q=query,
                    handle_addrs=handle_addrs,
                    before_height=before_height,
                    before_n=before_n,
                    limit=room,
                )
            elif before_height is None:
                # Page is full of unconfirmed trades. Older confirmed rows still exist.
                probe = self._candidate_batch(None, None, 1)
                has_more = bool(probe)
                if has_more and probe:
                    tip = self.tip_height()
                    next_h = (tip if tip >= 0 else 0) + 1
                    next_n = 0
        if before_height is None:
            seen = {t["txid"] for t in items}
            for trade in confirmed:
                if trade["txid"] not in seen:
                    items.append(trade)
        else:
            items.extend(confirmed)
        return {
            "items": items,
            "stats": self.stats(now=now, pending=pending),
            "has_more": has_more,
            "next_before": next_h,
            "next_before_n": next_n,
            "proceeds": list(self.proceeds),
        }
