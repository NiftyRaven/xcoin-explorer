"""Launch buy/sell detection for the public Trades page.

Launch (the bonding-curve market) is not in this repo. There is no fills
client here, so a trade is recognized from the chain:

* a memo-less swap still needs one configured Launch address on one side
* an ``XL1`` buy must pay the 0.60% platform fee to the Launch treasury
* the curve-net output of that buy is that listing's own reserve
* an ``XL1`` sell is paid by that learned reserve, or by a configured
  Launch address, and the asset goes to a Launch address
* change back to the sender is netted out and is not counted as a payment
* the network fee is reported on its own

The known proceeds, reserve, and treasury addresses are the default.
``explorer.toml`` ``[launch]`` or ``XFER_LAUNCH_PROCEEDS`` replaces that
list. Each new listing brings its own reserve, so a fixed list is not
enough. A verified buy (treasury fee, then a token delivery from the
treasury) teaches that reserve. A copied memo with no treasury fee is
not a trade. Only a Launch address can deliver the tokens.

The public page shows only the current America/New_York civil day, from
12:00 AM. Classified trades are kept in memory and dropped at the next
midnight. This page does not write a trade history table.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from explorer.amounts import format_xfer, xfer_to_atoms
from explorer.chain import COIN, classify_asset_name
from explorer.decode import parse_vout_script
from explorer.queries import norm_handle

# 12:00 AM America/New_York. zoneinfo applies EST/EDT, including the
# spring-forward and fall-back days (the odd hour is at 2:00 AM, not midnight).
ET = ZoneInfo("America/New_York")

# Launch fill marker in an OP_RETURN. ``XL1|B|NAME|xferons|tokens`` or ``XL1|S|...``.
# Buys store the curve net (after the 0.60% platform fee and 0.30% creator fee).
# Sells store the curve payout before those fees.
FILL_TAG = "XL1"
LAUNCH_PARENT = "LAUNCH_XFER"
# 10_000 - 60 bps platform - 30 bps creator. The buyer pays gross; the memo stores net.
_FAIR_KEEP_BPS = 10_000 - 60 - 30
_PLATFORM_FEE_BPS = 60

# Treasury receives the 0.60% platform fee on a buy and holds listing inventory.
# Every listing pays that fee here, then uses its own reserve for the curve net.
LAUNCH_TREASURY = "XmLv1ZYu8qMsGTsWvD9N7C7AFcK844nHwF"

# Configured Launch parties. The MY_TOKEN reserve is in this list. A newer
# listing's reserve is learned from a verified buy and does not have to be.
DEFAULT_LAUNCH_PROCEEDS = (
    "XvmKQ4Rf1PtETDRMaaCeVgtKmGqqieYfQF",
    "XgjkWe3SvSRTiTJg8YoZWx9feikvsqJpGo",
    LAUNCH_TREASURY,
)

# How often to rebuild today's in-memory list and re-read the mempool.
# Confirmed rows are read from the chain index for this window only.
# The classified list is not written to disk.
MEMPOOL_CACHE_SECONDS = 8.0


def et_midnight(now: int | float) -> tuple[int, str]:
    """Unix time of 12:00 AM ET on the civil day that contains ``now``, plus YYYY-MM-DD.

    ``fold=0`` picks the first instant of that clock time. Midnight itself is
    not in the repeated hour on a fall-back day, and it exists on a spring-forward day.
    """
    moment = datetime.fromtimestamp(int(now), tz=ET)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    return int(start.timestamp()), start.date().isoformat()


def et_day_window(now: int | float) -> tuple[int, int, str]:
    """``[start, end)`` unix seconds for the ET civil day containing ``now``.

    ``end`` is the next 12:00 AM ET. Adding 26 hours clears both a 23-hour
    spring-forward day and a 25-hour fall-back day before snapping to midnight.
    """
    start, key = et_midnight(now)
    end, _ = et_midnight(start + 26 * 3600)
    return start, end, key


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


def _memo_text(vout: dict) -> str | None:
    raw = vout.get("op_return")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if "XL1" in text:
        return text[text.index("XL1") :]
    try:
        if len(text) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in text):
            decoded = bytes.fromhex(text).decode("utf-8").strip()
        else:
            return None
    except (ValueError, UnicodeError):
        return None
    if "XL1" not in decoded:
        return None
    return decoded[decoded.index("XL1") :]


def decode_fill_memo(text: str) -> dict | None:
    """Parse one ``XL1`` fill.

    ``NAME`` with no slash is a ``LAUNCH_XFER`` child. ``*NAME`` is that
    exact asset (a new root, or a root unique). A field that already
    contains ``/`` or ``#`` is the chain name: a sub-asset of any root,
    or a unique NFT.
    """
    parts = (text or "").strip().split("|")
    if len(parts) != 5 or parts[0] != FILL_TAG:
        return None
    side = {"B": "buy", "S": "sell"}.get(parts[1])
    if not side:
        return None
    field = parts[2]
    if field.startswith("*"):
        name = field[1:]
    elif "/" in field or "#" in field:
        name = field
    else:
        name = f"{LAUNCH_PARENT}/{field}"
    if not name:
        return None
    try:
        xferons = int(parts[3])
        tokens = int(parts[4])
    except ValueError:
        return None
    if xferons <= 0 or tokens <= 0:
        return None
    return {"side": side, "asset": name, "net_atoms": xferons, "tokens": tokens}


def _fill_memos(tx: dict) -> list[dict]:
    found = []
    for row in tx.get("vout") or []:
        text = _memo_text(row)
        if not text:
            continue
        memo = decode_fill_memo(text)
        if memo:
            found.append(memo)
    return found


def _scale_tokens(tokens: int, units: int) -> int:
    units = int(units)
    if units < 0 or units > 8:
        return 0
    return int(tokens) * 10 ** (8 - units)


def _infer_units(tokens: int, moved: int) -> int | None:
    if tokens <= 0 or moved <= 0:
        return None
    for units in range(0, 9):
        if tokens * 10 ** (8 - units) == moved:
            return units
    return None


def _asset_aliases(name: str) -> set[str]:
    """Memo short name and ``LAUNCH_XFER/<short>`` name the same asset.

    A sub-asset of another root (``ROOT/CHILD``) stays that full name.
    It does not collapse onto a ``LAUNCH_XFER`` child that shares the suffix.
    """
    text = (name or "").strip()
    if text.startswith("*"):
        text = text[1:]
    if not text:
        return set()
    names = {text}
    parent = f"{LAUNCH_PARENT}/"
    if text.startswith(parent):
        short = text[len(parent) :]
        if short and "/" not in short and "#" not in short:
            names.add(short)
    elif "/" not in text and "#" not in text:
        names.add(f"{LAUNCH_PARENT}/{text}")
    return names


def _same_asset(left: str, right: str) -> bool:
    return bool(_asset_aliases(left) & _asset_aliases(right))


def _units_for(asset_units: dict | None, name: str) -> int | None:
    """Decimal places. Unique names (``#``) are 0. A stored 0 on a fungible is unknown."""
    if asset_units:
        for alias in _asset_aliases(name):
            if alias not in asset_units:
                continue
            units = int(asset_units[alias] or 0)
            if units > 0:
                return units
    if "#" in (name or ""):
        return 0
    return None


def _fair_gross(net: int) -> int | None:
    """Buyer gross when the memo net is the curve amount after the 0.90% fees."""
    if net > 0 and (net * 10_000) % _FAIR_KEEP_BPS == 0:
        gross = net * 10_000 // _FAIR_KEEP_BPS
        if gross > net:
            return gross
    return None


def _pays_platform_fee(tx: dict, gross: int) -> bool:
    """True when an output pays the treasury 0.60% of ``gross``, within 1 atom."""
    expected = int(gross) * _PLATFORM_FEE_BPS // 10_000
    rounded = (int(gross) * _PLATFORM_FEE_BPS + 5_000) // 10_000
    if expected <= 0:
        return False
    for row in tx.get("vout") or []:
        if row.get("address") != LAUNCH_TREASURY:
            continue
        paid = _atoms(row.get("value"))
        if paid == expected or paid == rounded or abs(paid - expected) <= 1:
            return True
    return False


def _reserve_output(tx: dict, net: int) -> str | None:
    """Address that receives the curve net. That is this listing's reserve."""
    hits = []
    for row in tx.get("vout") or []:
        addr = row.get("address")
        if addr and addr != LAUNCH_TREASURY and _atoms(row.get("value")) == net:
            if addr not in hits:
                hits.append(addr)
    if len(hits) == 1:
        return hits[0]
    return None


def _xfer_spent(tx: dict) -> dict[str, int]:
    """XFER atoms each address put in, minus the XFER it took back as change."""
    entered: dict[str, int] = {}
    left: dict[str, int] = {}
    for row in tx.get("vin") or []:
        _add(entered, row.get("address"), _atoms(row.get("value")))
    for row in tx.get("vout") or []:
        _add(left, row.get("address"), _atoms(row.get("value")))
    spent: dict[str, int] = {}
    for addr in set(entered) | set(left):
        delta = entered.get(addr, 0) - left.get(addr, 0)
        if delta:
            spent[addr] = delta
    return spent


def _launch_delivered_in_tx(
    tx: dict,
    name: str,
    asset_atoms: int,
    buyer: str,
    allowed: set[str],
) -> bool:
    """True when this tx itself sends the memo amount from a Launch input to the buyer."""
    if not buyer or asset_atoms <= 0:
        return False
    from_launch = any(
        row.get("address") in allowed
        and _same_asset(row.get("asset") or "", name)
        and int(row.get("asset_amount") or 0) > 0
        for row in tx.get("vin") or []
    )
    if not from_launch:
        return False
    return any(
        row.get("address") == buyer
        and _same_asset(row.get("asset") or "", name)
        and int(row.get("asset_amount") or 0) == asset_atoms
        for row in tx.get("vout") or []
    )


def _is_token_delivery(
    view: dict,
    asset: str,
    amount: int,
    buyer: str,
    launch: set[str],
) -> str | None:
    """Chain asset name when Launch sends exactly ``amount`` of it to ``buyer``."""
    if not view or view.get("coinbase") or not buyer or amount <= 0:
        return None
    if _fill_memos(view):
        return None
    from_launch = any(
        row.get("address") in launch
        and _same_asset(row.get("asset") or "", asset)
        and int(row.get("asset_amount") or 0) > 0
        for row in view.get("vin") or []
    )
    if not from_launch:
        return None
    for row in view.get("vout") or []:
        held = row.get("asset") or ""
        if (
            row.get("address") == buyer
            and _same_asset(held, asset)
            and int(row.get("asset_amount") or 0) == int(amount)
        ):
            return held or asset
    return None


def _tx_after_key(view: dict) -> tuple:
    height = view.get("height")
    if height is None:
        return (10**18, 0, view.get("txid") or "")
    return (int(height), int(view.get("n") or 0), view.get("txid") or "")


def _lone_sell_memo(view: dict) -> bool:
    memos = _fill_memos(view)
    return len(memos) == 1 and memos[0].get("side") == "sell"


def _sell_trader(
    tx: dict,
    name: str,
    asset_atoms: int,
    moved: list,
    excluded: set[str],
) -> str | None:
    """Address that sold the tokens.

    Configured Launch addresses, the treasury, and a learned listing reserve
    are left out. A combined Launch tx can move a much larger inventory from
    one of those addresses in the same transaction as the fill.
    """
    spent: dict[str, int] = {}
    for row in tx.get("vin") or []:
        addr = row.get("address")
        held = row.get("asset") or ""
        amount = int(row.get("asset_amount") or 0)
        if not addr or addr in excluded or amount <= 0 or not _same_asset(held, name):
            continue
        spent[addr] = spent.get(addr, 0) + amount
    if not spent:
        return None
    net_loss = {
        addr: -amount
        for addr, amount, _held in moved
        if amount < 0 and addr in spent
    }
    exact = [addr for addr, loss in net_loss.items() if loss == asset_atoms]
    if len(exact) == 1:
        return exact[0]
    if len(spent) == 1:
        return next(iter(spent))
    return max(spent, key=lambda addr: (net_loss.get(addr, 0), spent[addr]))


def _tx_after(delivery: dict, buy: dict) -> bool:
    """Delivery is in the same block, a later block, or the mempool.

    Launch sometimes places the treasury transfer ahead of the buy in the
    same block, so tx order inside the block does not matter.
    """
    if not delivery or not buy or delivery.get("txid") == buy.get("txid"):
        return False
    dh = delivery.get("height")
    bh = buy.get("height")
    if dh is None and bh is None:
        return True
    if dh is None:
        return bh is not None
    if bh is None:
        return False
    return int(dh) >= int(bh)


def _trade_from_memo(
    memo: dict,
    tx: dict,
    xfer_net: dict[str, int],
    asset_net: dict[str, dict[str, int]],
    fee: int,
    asset_units: dict | None,
    allowed: set[str],
    reserves: dict | None = None,
) -> dict | None:
    """One Launch fill marked with XL1. Extra fee and change outputs are allowed.

    A buy counts when the treasury is paid the 0.60% platform fee and some
    output pays the memo net (that address is the listing reserve). Tokens
    stay "on the way" until a Launch address delivers them. A sell counts
    when the asset arrives at a configured Launch address and the XFER
    payout is spent by that asset's learned reserve or a configured Launch
    address. The seller is whoever else spent the asset: Launch addresses,
    the treasury, and the learned reserve are not the trader.
    """
    if not allowed:
        return None
    name = memo["asset"]
    moved = []
    for addr, nets in asset_net.items():
        for held, amount in nets.items():
            if amount and _same_asset(held, name):
                moved.append((addr, amount, held))
    units = _units_for(asset_units, name)
    inferred = None
    for _addr, amount, _held in moved:
        got = _infer_units(memo["tokens"], abs(amount))
        if got is not None:
            inferred = got
            break
    if units is None:
        units = inferred
    if units is None:
        return None
    asset_atoms = _scale_tokens(memo["tokens"], units)
    if asset_atoms <= 0:
        return None

    if memo["side"] == "buy":
        net = int(memo["net_atoms"])
        gross = _fair_gross(net)
        if gross is None or not _pays_platform_fee(tx, gross):
            return None
        reserve = _reserve_output(tx, net)
        if not reserve:
            return None
        losses = [(addr, -amount) for addr, amount in xfer_net.items() if amount < 0 and addr]
        if not losses:
            return None
        target = gross + fee
        losses.sort(key=lambda item: (abs(item[1] - target), -item[1]))
        trader = losses[0][0]
        tokens_pending = not _launch_delivered_in_tx(tx, name, asset_atoms, trader, allowed)
        counterparty = reserve
        xfer_atoms = gross
    else:
        if inferred is None and not any(abs(amount) == asset_atoms for _addr, amount, _held in moved):
            return None
        gainers = [addr for addr, amount, _held in moved if amount == asset_atoms and addr in allowed]
        if not gainers:
            return None
        payers = set(allowed)
        for alias in _asset_aliases(name):
            learned = (reserves or {}).get(alias)
            if learned:
                payers.add(learned)
        launch_inputs = [row.get("address") for row in (tx.get("vin") or []) if row.get("address") in payers]
        if not launch_inputs:
            return None
        payout = int(memo["net_atoms"])
        if not any(addr in payers and spent == payout for addr, spent in _xfer_spent(tx).items()):
            return None
        trader = _sell_trader(tx, name, asset_atoms, moved, set(payers) | {LAUNCH_TREASURY})
        if not trader:
            return None
        counterparty = gainers[0]
        for addr, amount, held in moved:
            if addr in gainers and amount == asset_atoms and held:
                name = held
                break
        xfer_atoms = payout
        tokens_pending = False
        reserve = ""

    if not trader or xfer_atoms <= 0:
        return None
    trade = {
        "side": memo["side"],
        "asset": name,
        "asset_type": classify_asset_name(name),
        "asset_atoms": asset_atoms,
        "xfer_atoms": xfer_atoms,
        "fee_atoms": fee,
        "price_atoms": price_per_unit_atoms(xfer_atoms, asset_atoms),
        "trader": trader,
        "counterparty": counterparty or "",
        "reserve": reserve or "",
        "tokens_pending": tokens_pending,
        "delivery_txid": None,
    }
    trade["sentence"] = describe_trade(
        trade["side"], short_address(trader), asset_atoms, name, xfer_atoms
    )
    return trade


def classify_launch_trade(
    tx: dict,
    proceeds: Any = None,
    *,
    asset_units: dict | None = None,
    reserves: dict | None = None,
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

    memos = _fill_memos(tx)
    if len(memos) > 1:
        return None
    if len(memos) == 1:
        return _trade_from_memo(
            memos[0], tx, xfer_net, asset_net, fee, asset_units, allowed, reserves
        )

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
        op_return = None
        if spk.get("type") == "nulldata" or str(spk.get("hex") or "").startswith("6a"):
            parsed = parse_vout_script(spk.get("hex") or "")
            op_return = parsed.get("op_return") or None
        vout.append(
            {
                "address": address,
                "value": _atoms(vout_row.get("value")),
                "asset": name,
                "asset_amount": amount,
                "asset_kind": kind,
                "op_return": op_return,
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
        # In-memory only. Never written to sqlite.
        self._day_key: str | None = None
        self._day_trades: list[dict] = []
        self._day_built: float = 0.0
        # Assets whose reserve was already sought and not found. Skips another
        # op_return scan and another round of block fetches on the next page.
        self._reserve_misses: set[str] = set()

    def _asset_units(self) -> dict[str, int]:
        """Decimal places for assets whose issue script recorded units > 0."""
        try:
            rows = self.db.conn.execute("SELECT name, units FROM assets").fetchall()
        except Exception:
            return {}
        out: dict[str, int] = {}
        for row in rows:
            units = int(row["units"] or 0)
            if row["name"] and units > 0:
                for alias in _asset_aliases(row["name"]):
                    out.setdefault(alias, units)
        return out

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
        # A buy is confirmed only after the treasury delivery is in a block.
        if base.get("side") == "buy" and base.get("tokens_pending"):
            confirmed = False
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
                    "op_return": row["op_return"] if "op_return" in row.keys() else None,
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
            base = classify_launch_trade(
                view,
                self.proceeds,
                asset_units=self._asset_units(),
                reserves=self._load_reserves(),
            )
            if not base:
                continue
            trades.append(self._decorate(base, view, self._handles([base["trader"]])))
        with self._lock:
            self._mem = (clock, key, trades)
        return trades

    def _begin_day(self, now: int) -> tuple[int, int, str]:
        """Drop yesterday's in-memory trades when the ET date changes."""
        start, end, key = et_day_window(now)
        with self._lock:
            if self._day_key != key:
                self._day_key = key
                self._day_trades = []
                self._day_built = 0.0
                self._mem = None
                self._reserve_misses = set()
        return start, end, key

    def _trades_between(self, start: int, end: int) -> list[dict]:
        """Read chain rows in ``[start, end)``. Does not write a trade table."""
        if not self.proceeds:
            return []
        marks, params = self._proceeds_sql()
        rows = self.db.conn.execute(
            f"""
            SELECT t.txid, t.height, t.n, t.time, t.fee, t.coinbase
            FROM txs t
            WHERE t.coinbase = 0 AND t.time >= ? AND t.time < ?
              AND EXISTS (
                SELECT 1 FROM txio i
                WHERE i.txid = t.txid AND i.address IN ({marks})
              )
            ORDER BY t.height DESC, t.n DESC
            """,
            [start, end, *params],
        ).fetchall()
        metas = [dict(r) for r in rows]
        views = self._load_views(metas)
        ordered = []
        for meta in metas:
            view = views.get(meta["txid"])
            if not view:
                continue
            when = int(view.get("time") or 0)
            if when < start or when >= end:
                continue
            ordered.append(view)
        return self._assemble(ordered)

    def _load_reserves(self) -> dict[str, str]:
        try:
            rows = self.db.conn.execute("SELECT asset, address FROM launch_reserves").fetchall()
        except Exception:
            return {}
        out: dict[str, str] = {}
        for row in rows:
            if row["asset"] and row["address"]:
                for alias in _asset_aliases(row["asset"]):
                    out.setdefault(alias, row["address"])
        return out

    def _remember_reserve(self, reserves: dict[str, str], asset: str, address: str, txid: str | None) -> None:
        """Record a verified reserve. The first address for an asset is kept."""
        if not asset or not address:
            return
        aliases = list(_asset_aliases(asset))
        known = next((reserves[alias] for alias in aliases if reserves.get(alias)), None)
        if known is None:
            try:
                marks = ",".join("?" * len(aliases))
                row = self.db.conn.execute(
                    f"SELECT address FROM launch_reserves WHERE asset IN ({marks}) LIMIT 1",
                    aliases,
                ).fetchone()
            except Exception:
                row = None
            if row and row["address"]:
                known = row["address"]
        if known:
            for alias in aliases:
                reserves.setdefault(alias, known)
            return
        for alias in aliases:
            reserves[alias] = address
        self._reserve_misses.difference_update(aliases)
        try:
            self.db.conn.execute(
                "INSERT OR IGNORE INTO launch_reserves(asset, address, txid) VALUES(?,?,?)",
                (asset, address, txid or None),
            )
            self.db.commit()
        except Exception:
            return

    def _attach_deliveries(self, buys: list[tuple[dict, dict]], views: list[dict]) -> None:
        """Clear tokens-on-the-way once a later Launch delivery of that exact amount is in a block."""
        launch = set(self.proceeds) | {LAUNCH_TREASURY}
        used: set[str] = set()
        ordered = sorted(buys, key=lambda pair: _tx_after_key(pair[0]))
        candidates = sorted(views, key=_tx_after_key)
        for view, base in ordered:
            if base.get("side") != "buy" or not base.get("tokens_pending"):
                continue
            for cand in candidates:
                txid = cand.get("txid") or ""
                if not txid or txid in used or txid == view.get("txid"):
                    continue
                if not _tx_after(cand, view):
                    continue
                delivered = _is_token_delivery(
                    cand, base["asset"], int(base["asset_atoms"]), base["trader"], launch
                )
                if not delivered:
                    continue
                if cand.get("height") is None:
                    base["delivery_txid"] = txid
                    continue
                base["asset"] = delivered
                base["asset_type"] = classify_asset_name(delivered)
                base["tokens_pending"] = False
                base["delivery_txid"] = txid
                used.add(txid)
                break

    def _assemble(self, views: list[dict]) -> list[dict]:
        """Buys first, so a verified delivery can teach the reserve before sells are judged."""
        reserves = self._load_reserves()
        units = self._asset_units()
        buys: list[tuple[dict, dict]] = []
        sells: list[tuple[dict, dict]] = []
        retry: list[dict] = []
        for view in views:
            base = classify_launch_trade(
                view, self.proceeds, asset_units=units, reserves=reserves
            )
            if base and base.get("side") == "buy":
                buys.append((view, base))
            elif base and base.get("side") == "sell":
                sells.append((view, base))
            elif _lone_sell_memo(view):
                retry.append(view)
        self._attach_deliveries(buys, views)
        confirmed = [
            (view, base)
            for view, base in buys
            if not base.get("tokens_pending") and base.get("reserve")
        ]
        for view, base in sorted(confirmed, key=lambda pair: _tx_after_key(pair[0])):
            self._remember_reserve(reserves, base["asset"], base["reserve"], view.get("txid"))
        if retry:
            needed = set()
            for view in retry:
                memo = _fill_memos(view)[0]
                if not any(alias in reserves for alias in _asset_aliases(memo["asset"])):
                    needed.add(memo["asset"])
            self._seed_reserves_from_index(needed, reserves)
            for view in retry:
                base = classify_launch_trade(
                    view, self.proceeds, asset_units=units, reserves=reserves
                )
                if base:
                    sells.append((view, base))
        found = buys + sells
        handles = self._handles([base["trader"] for _, base in found])
        return [self._decorate(base, view, handles) for view, base in found]

    def _asset_indexed(self, asset: str) -> bool:
        aliases = list(_asset_aliases(asset))
        if not aliases:
            return False
        marks = ",".join("?" * len(aliases))
        try:
            row = self.db.conn.execute(
                f"SELECT 1 FROM assets WHERE name IN ({marks}) LIMIT 1",
                aliases,
            ).fetchone()
        except Exception:
            return False
        return row is not None

    def _seed_reserves_from_index(self, assets: set[str], reserves: dict[str, str]) -> None:
        """Older verified buys live in the chain index. Read them; do not rebuild it.

        An asset with no index row, or one already looked up and missing,
        does not scan ``op_return`` and does not fetch blocks.
        """
        for asset in assets:
            aliases = _asset_aliases(asset)
            if any(alias in reserves for alias in aliases):
                continue
            if aliases and aliases <= self._reserve_misses:
                continue
            if not self._asset_indexed(asset):
                continue
            found = self._reserve_from_index(asset)
            if not found:
                self._fill_missing_memos(asset)
                found = self._reserve_from_index(asset)
            if found:
                address, txid = found
                self._remember_reserve(reserves, asset, address, txid)
            else:
                self._reserve_misses.update(aliases)

    def _reserve_from_index(self, asset: str) -> tuple[str, str] | None:
        patterns: list[str] = []
        for alias in _asset_aliases(asset):
            plain = f"XL1|B|{alias}|"
            patterns.append(f"%{plain}%")
            patterns.append(f"%{plain.encode().hex()}%")
        seen: list[str] = []
        for pattern in patterns:
            rows = self.db.conn.execute(
                """
                SELECT DISTINCT txid FROM txio
                WHERE direction='out' AND ifnull(op_return, '') LIKE ?
                LIMIT 20
                """,
                (pattern,),
            ).fetchall()
            for row in rows:
                if row["txid"] not in seen:
                    seen.append(row["txid"])
        if not seen:
            return None
        marks = ",".join("?" * len(seen))
        metas = [
            dict(row)
            for row in self.db.conn.execute(
                f"""
                SELECT txid, height, n, time, coinbase FROM txs
                WHERE txid IN ({marks})
                ORDER BY height, n
                """,
                seen,
            ).fetchall()
        ]
        views = self._load_views(metas)
        units = self._asset_units()
        for meta in metas:
            view = views.get(meta["txid"])
            if not view:
                continue
            base = classify_launch_trade(view, self.proceeds, asset_units=units)
            if not base or base.get("side") != "buy" or not base.get("reserve"):
                continue
            if not _same_asset(base["asset"], asset):
                continue
            if self._delivery_in_index(base, view):
                return base["reserve"], view.get("txid") or ""
        return None

    def _delivery_in_index(self, buy: dict, buy_view: dict) -> bool:
        aliases = list(_asset_aliases(buy["asset"]))
        marks = ",".join("?" * len(aliases))
        rows = self.db.conn.execute(
            f"""
            SELECT t.txid, t.height, t.n, t.time, t.coinbase
            FROM txs t
            JOIN txio o ON o.txid = t.txid AND o.direction = 'out'
              AND o.address = ? AND o.asset_amount = ? AND o.asset IN ({marks})
            JOIN txio i ON i.txid = t.txid AND i.direction = 'in'
              AND i.address = ? AND ifnull(i.asset_amount, 0) > 0 AND i.asset IN ({marks})
            ORDER BY t.height, t.n
            LIMIT 8
            """,
            [buy["trader"], int(buy["asset_atoms"]), *aliases, LAUNCH_TREASURY, *aliases],
        ).fetchall()
        for row in rows:
            view = {
                "txid": row["txid"],
                "height": row["height"],
                "n": row["n"],
                "time": row["time"],
                "coinbase": bool(row["coinbase"]),
            }
            if _tx_after(view, buy_view):
                return True
        return False

    def _fill_missing_memos(self, asset: str) -> None:
        """Copy OP_RETURN onto older rows for this asset. Does not delete anything."""
        rpc = self.rpc
        if rpc is None or not getattr(rpc, "connected", False):
            return
        aliases = list(_asset_aliases(asset))
        if not aliases:
            return
        marks = ",".join("?" * len(aliases))
        heights = [
            int(row["height"])
            for row in self.db.conn.execute(
                f"""
                SELECT DISTINCT t.height AS height
                FROM txs t
                JOIN txio a ON a.txid = t.txid AND a.asset IN ({marks})
                JOIN txio n ON n.txid = t.txid AND n.direction = 'out'
                  AND n.script_type = 'nulldata' AND n.op_return IS NULL
                WHERE t.height IS NOT NULL
                ORDER BY t.height DESC
                LIMIT 15
                """,
                aliases,
            ).fetchall()
        ]
        for height in heights:
            block_hash = rpc.try_call("getblockhash", height, default=None)
            block = rpc.try_call("getblock", block_hash, 2, default=None) if block_hash else None
            if not isinstance(block, dict):
                continue
            for tx in block.get("tx") or []:
                if not isinstance(tx, dict) or not tx.get("txid"):
                    continue
                for vout in tx.get("vout") or []:
                    spk = vout.get("scriptPubKey") or {}
                    if spk.get("type") != "nulldata" and not str(spk.get("hex") or "").startswith("6a"):
                        continue
                    parsed = parse_vout_script(spk.get("hex") or "")
                    payload = parsed.get("op_return")
                    if payload is None:
                        continue
                    self.db.conn.execute(
                        """
                        UPDATE txio SET op_return=?
                        WHERE txid=? AND n=? AND direction='out' AND op_return IS NULL
                        """,
                        (payload, tx["txid"], int(vout.get("n") or 0)),
                    )
        if heights:
            self.db.commit()

    def today_confirmed(self, now: int) -> list[dict]:
        start, end, key = self._begin_day(now)
        mono = time.monotonic()
        with self._lock:
            if self._day_key == key and self._day_built and mono - self._day_built < self.cache_seconds:
                return list(self._day_trades)
        trades = self._trades_between(start, end)
        with self._lock:
            if self._day_key != key:
                # Midnight passed while the chain was being read. Don't keep the old day.
                return []
            self._day_trades = trades
            self._day_built = time.monotonic()
        return list(trades)

    def _pending_today(self, now: int, start: int, end: int) -> list[dict]:
        rows = []
        for trade in self.mempool_trades():
            when = trade.get("time")
            if when is None:
                when = now
            try:
                when_i = int(when)
            except (TypeError, ValueError):
                when_i = now
            if start <= when_i < end:
                rows.append(trade)
        return rows

    def stats(self, *, now: int | None = None, pending: list[dict] | None = None) -> dict:
        moment = int(time.time() if now is None else now)
        start, end, key = et_day_window(moment)
        confirmed = self.today_confirmed(moment)
        pending_rows = self._pending_today(moment, start, end) if pending is None else pending
        seen = {t["txid"] for t in confirmed}
        volume = 0
        count = 0
        waiting = 0
        for trade in confirmed:
            count += 1
            volume += int(trade.get("xfer_atoms") or 0)
            if trade.get("tokens_pending"):
                waiting += 1
        for trade in pending_rows:
            if trade.get("txid") in seen:
                continue
            count += 1
            volume += int(trade.get("xfer_atoms") or 0)
        return {
            "trades_today": count,
            "volume_today_atoms": volume,
            "pending": len(pending_rows) + waiting,
            "day_start": start,
            "day": key,
        }

    def page(
        self,
        *,
        side: str = "all",
        q: str = "",
        now: int | None = None,
    ) -> dict:
        """Today's Launch trades, newest first. No history before 12:00 AM ET."""
        moment = int(time.time() if now is None else now)
        start, end, key = self._begin_day(moment)
        side = side if side in ("buy", "sell") else "all"
        query = (q or "").strip()[:200]
        handle_addrs: set[str] = set()
        if query:
            handle_addrs = self._handle_addresses(query)
        pending = self._pending_today(moment, start, end)
        confirmed = self.today_confirmed(moment)
        items: list[dict] = []
        seen: set[str] = set()
        for trade in list(pending) + list(confirmed):
            txid = trade.get("txid") or ""
            if txid in seen:
                continue
            if not self._match(trade, side, query, handle_addrs):
                continue
            seen.add(txid)
            items.append(trade)
        return {
            "items": items,
            "stats": self.stats(now=moment, pending=pending),
            "has_more": False,
            "day_start": start,
            "day_end": end,
            "day": key,
            "proceeds": list(self.proceeds),
        }
