"""Why a handle is in the hat, or not.

The node does not report why a check-in was dropped. Each check uses data
the explorer already has. When the result is an inference, ``likely`` is
true and the text says so.
"""

from __future__ import annotations

import time
from typing import Any

HEARTBEAT_LIMIT_SECONDS = 180
# A check-in this long after the last hat block kept arriving once the name was out.
_CONTINUED_SECONDS = 90

FIX_VPN = (
    "Keep the VPN on. Choose one stable P2P-friendly server that allows outbound TCP 38443, "
    "with no auto IP rotation or server hopping, and enable auto-reconnect. "
    "Run one wallet per handle, keep the PC awake, and keep the wallet synced to the tip."
)
FIX_HEARTBEAT = (
    "Open one wallet and leave the PC awake. A check-in older than 180 seconds does not count."
)
FIX_SYNC = "Keep one wallet open, awake, and synced to the tip so the next check-in can be counted."
FIX_VERIFY = "Sign in with X on an account with a blue check. A name without that check is not drawn."
FIX_STAMP = (
    "A check-in is arriving, but this payout is not stamped as a live blue check. "
    "Sign in with X and leave one wallet open."
)
FIX_NEXT = "Leave this one wallet open and synced. It can join the next draw."
FIX_OTHER = "Run one wallet for this handle. A second wallet can keep check-ins from being counted."
FIX_NODE = "Sign in with X in the wallet so this name is linked to a node id."
FIX_OPEN = "Open the wallet, leave it unlocked, and let it sync to the tip."

NOTE = (
    "This node does not report why a check-in was left out. "
    "A line marked likely is the strongest reading of the data the explorer can see."
)

_VIA_RANK = {"payout": 0, "identity": 1, "node": 2, "node_id": 2, "link": 3}


def _fmt_time(ts: int | None) -> str:
    if ts is None:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(ts)))


def _age_phrase(age: int) -> str:
    age = max(0, int(age))
    if age < 90:
        unit = "second" if age == 1 else "seconds"
        return f"{age} {unit} ago"
    if age < 3600:
        minutes = max(1, age // 60)
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit} ago"
    if age < 86400:
        hours = max(1, age // 3600)
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago"
    days = max(1, age // 86400)
    unit = "day" if days == 1 else "days"
    return f"{days} {unit} ago"


def _check(
    check_id: str,
    label: str,
    tip: str,
    source: str,
    *,
    ok: bool | None,
    detail: str,
    fix: str | None = None,
    likely: bool = False,
    extra: dict | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "tip": tip,
        "source": source,
        "ok": ok,
        "likely": bool(likely),
        "detail": detail,
        "fix": fix if ok is False else None,
    }
    if extra:
        row.update(extra)
    return row


def _short_id(node_id: str | None) -> str:
    text = node_id or ""
    if len(text) <= 12:
        return text or "unknown"
    return text[:8] + "…"


def pick_subject(matches: list[dict]) -> dict | None:
    """Choose the handle an address or node id most likely belongs to."""
    if not matches:
        return None

    def key(row: dict) -> tuple:
        return (_VIA_RANK.get(row.get("via") or "", 9), -int(row.get("height") or 0), row.get("handle") or "")

    return sorted(matches, key=key)[0]


def assemble(
    index: dict,
    *,
    hat_handles: set[str],
    hat_source: str,
    live_nodes: list[dict],
    live: dict | None,
    now: int | None = None,
    tip_height: int | None = None,
    tip_time: int | None = None,
    query: str = "",
    via: str = "handle",
    also: list[dict] | None = None,
) -> dict[str, Any]:
    """Merge the index with this minute's heartbeats and build the checklist."""
    if now is None:
        now = int(time.time())
    handle = index.get("handle") or ""
    hat = {h for h in hat_handles if h}
    in_hat = bool(handle) and handle in hat
    ours = [n for n in live_nodes if n.get("handle") == handle and n.get("id")]
    live_ids = []
    seen_ids: set[str] = set()
    for node in ours:
        nid = str(node.get("id") or "").lower()
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            live_ids.append(nid)
    flags = [n.get("attested") for n in ours if isinstance(n.get("attested"), bool)]
    attested: bool | None
    if any(flag is True for flag in flags):
        attested = True
    elif flags:
        attested = False
    else:
        attested = None
    local_verified = None
    local_attested = None
    if isinstance(live, dict):
        local_handle = (live.get("local_xaccount") or "").strip().lower().lstrip("@")
        if local_handle and local_handle == handle:
            if isinstance(live.get("local_x_verified"), bool):
                local_verified = bool(live.get("local_x_verified"))
            if isinstance(live.get("local_attested"), bool):
                local_attested = bool(live.get("local_attested"))
    if attested is None and local_attested is not None:
        attested = local_attested
    live_seen = [int(n["lastseen"]) for n in ours if isinstance(n.get("lastseen"), int)]
    index_seen = index.get("last_seen") if isinstance(index.get("last_seen"), int) else None
    last_heartbeat = None
    heartbeat_source = None
    if live_seen and (index_seen is None or max(live_seen) >= index_seen):
        last_heartbeat = max(live_seen)
        heartbeat_source = "getactivenodes"
    elif index_seen is not None:
        last_heartbeat = index_seen
        heartbeat_source = "node_seen"
    elif live_seen:
        last_heartbeat = max(live_seen)
        heartbeat_source = "getactivenodes"
    our_node_ids = {str(n).lower() for n in (index.get("node_ids") or []) if n}
    others: list[dict] = []
    seen_other: set[str] = set()

    def add_other(other: str, how: str, node_id: str | None = None, address: str | None = None) -> None:
        name = (other or "").strip().lower().lstrip("@")
        if not name or name == handle or name in seen_other:
            return
        seen_other.add(name)
        others.append(
            {
                "handle": name,
                "via": how,
                "node_id": (node_id or None),
                "address": address or None,
                "in_hat": name in hat,
            }
        )

    for row in index.get("others") or []:
        add_other(row.get("handle") or "", row.get("via") or "node_id", row.get("node_id"), row.get("address"))
    for node in live_nodes:
        nid = str(node.get("id") or "").lower()
        other = node.get("handle") or ""
        if nid and nid in our_node_ids and other and other != handle:
            add_other(other, "node_id", nid, None)
    others.sort(key=lambda row: (not row["in_hat"], 0 if row["via"] == "node_id" else 1, row["handle"]))
    others = others[:5]
    indexed_tip = index.get("tip_height")
    if not isinstance(indexed_tip, int):
        indexed_tip = -1
    if tip_height is None:
        tip_height = indexed_tip
    else:
        tip_height = max(int(tip_height), indexed_tip)
    if tip_time is None:
        tip_time = index.get("tip_time") if isinstance(index.get("tip_time"), int) else None
    body = {
        "query": query or (f"@{handle}" if handle else ""),
        "handle": handle or None,
        "found": bool(index.get("found") or in_hat or ours or last_heartbeat is not None),
        "via": via,
        "also": also or [],
        "identity_address": index.get("identity_address"),
        "payout_address": index.get("payout_address"),
        "registered_node_id": index.get("registered_node_id"),
        "live_node_ids": live_ids,
        "tip_height": tip_height if tip_height >= 0 else None,
        "tip_time": tip_time,
        "in_hat": in_hat,
        "heartbeat_limit_seconds": HEARTBEAT_LIMIT_SECONDS,
        "rejection_reasons_exposed": False,
        "note": NOTE,
        "last_heartbeat": last_heartbeat,
        "last_hat": index.get("last_hat"),
        "last_hat_time": index.get("last_hat_time"),
    }
    checks = _checks(
        handle=handle,
        found=body["found"],
        in_hat=in_hat,
        hat_source=hat_source,
        last_heartbeat=last_heartbeat,
        heartbeat_source=heartbeat_source,
        now=int(now),
        last_hat=index.get("last_hat") if isinstance(index.get("last_hat"), int) else None,
        last_hat_time=index.get("last_hat_time") if isinstance(index.get("last_hat_time"), int) else None,
        tip_height=body["tip_height"],
        tip_time=tip_time if isinstance(tip_time, int) else None,
        attested=attested,
        in_active=bool(ours),
        local_verified=local_verified,
        ever_in_hat=bool(index.get("ever_in_hat") or in_hat),
        others=others,
        live_ids=live_ids,
        registered=index.get("registered_node_id"),
        seen_node_ids=sorted(our_node_ids),
    )
    body["others"] = others
    body["checks"] = checks
    body["summary"] = _summary(body, checks)
    return body


def _checks(
    *,
    handle: str,
    found: bool,
    in_hat: bool,
    hat_source: str,
    last_heartbeat: int | None,
    heartbeat_source: str | None,
    now: int,
    last_hat: int | None,
    last_hat_time: int | None,
    tip_height: int | None,
    tip_time: int | None,
    attested: bool | None,
    in_active: bool,
    local_verified: bool | None,
    ever_in_hat: bool,
    others: list[dict],
    live_ids: list[str],
    registered: str | None,
    seen_node_ids: list[str],
) -> list[dict]:
    if not handle:
        return [
            _check(
                "in_hat",
                "In the hat",
                "Yes when this name is in the current minute’s draw.",
                hat_source or "none",
                ok=False,
                detail="No @handle or address matched that search.",
                fix="Search the @handle, or the address where wins are paid.",
            )
        ]
    age = None if last_heartbeat is None else max(0, now - int(last_heartbeat))
    fresh = age is not None and age <= HEARTBEAT_LIMIT_SECONDS
    continued = (
        not in_hat
        and last_heartbeat is not None
        and last_hat_time is not None
        and int(last_heartbeat) >= int(last_hat_time) + _CONTINUED_SECONDS
    )
    missed_counted = (fresh or continued) and attested is not False and (continued or not in_active or bool(others))
    restart = fresh and in_active and attested is True and not others and not continued
    return [
        _in_hat_check(in_hat, hat_source, fresh, continued, missed_counted, restart, attested, in_active, last_hat, last_hat_time),
        _heartbeat_check(age, last_heartbeat, heartbeat_source, fresh),
        _last_hat_check(in_hat, last_hat, last_hat_time, tip_height, tip_time, found),
        _verified_check(local_verified, attested, in_active, ever_in_hat, in_hat),
        _other_check(others, live_ids),
        _node_check(registered, live_ids, seen_node_ids),
    ]


def _in_hat_check(in_hat, hat_source, fresh, continued, missed_counted, restart, attested, in_active, last_hat, last_hat_time) -> dict:
    tip = "Yes when this name is in the node’s list for this minute’s draw. The node does not say why a name was left off that list."
    source = "getlotteryinfo.stamped_handles" if hat_source == "stamped_handles" else "last indexed hat"
    if in_hat:
        return _check("in_hat", "In the hat", tip, source, ok=True, detail="Yes. This name is in the current draw.")
    if fresh and attested is False:
        return _check(
            "in_hat",
            "In the hat",
            tip,
            source,
            ok=False,
            likely=False,
            detail="No. A check-in is in the active list, but this payout is not stamped for the draw.",
            fix=FIX_STAMP,
        )
    if not in_hat and missed_counted and (continued or (fresh and not restart)):
        when = ""
        if last_hat is not None and last_hat_time is not None:
            when = f" The last hat block was {last_hat} / {_fmt_time(last_hat_time)}."
        elif last_hat is not None:
            when = f" The last hat block was {last_hat}."
        if continued:
            detail = "No. Likely: check-ins were still arriving after this name left the hat, and they were not counted." + when
        else:
            detail = "No. Likely: a check-in is arriving, but it is not counted in this draw." + when
        return _check(
            "in_hat",
            "In the hat",
            tip,
            source,
            ok=False,
            likely=True,
            detail=detail,
            fix=FIX_VPN,
        )
    if restart:
        return _check(
            "in_hat",
            "In the hat",
            tip,
            source,
            ok=False,
            likely=True,
            detail="No. A stamped check-in is arriving, but this name is not in this minute’s draw. Likely it joined after this draw was set.",
            fix=FIX_NEXT,
        )
    if fresh and in_active:
        return _check(
            "in_hat",
            "In the hat",
            tip,
            source,
            ok=False,
            likely=True,
            detail="No. A check-in is arriving, but this name is not in this minute’s draw.",
            fix=FIX_VPN,
        )
    return _check(
        "in_hat",
        "In the hat",
        tip,
        source,
        ok=False,
        detail="No. This name is not in the current draw.",
        fix=FIX_OPEN,
    )


def _heartbeat_check(age, last_heartbeat, heartbeat_source, fresh) -> dict:
    tip = "The newest check-in this node can see. It has to be within 180 seconds to count."
    source = "getactivenodes.lastseen" if heartbeat_source == "getactivenodes" else "node_seen.last_seen"
    if heartbeat_source is None:
        source = "getactivenodes.lastseen or node_seen.last_seen"
    if last_heartbeat is None or age is None:
        return _check(
            "heartbeat",
            "Last heartbeat",
            tip,
            source,
            ok=False,
            detail="No check-in is stored for this name. The limit is 180 seconds.",
            fix=FIX_HEARTBEAT,
            extra={"age_seconds": None, "last_heartbeat": None},
        )
    stamp = _fmt_time(last_heartbeat)
    if fresh:
        detail = f"Yes. Last check-in was {_age_phrase(age)} ({stamp}). The limit is 180 seconds."
        return _check(
            "heartbeat",
            "Last heartbeat",
            tip,
            source,
            ok=True,
            detail=detail,
            extra={"age_seconds": age, "last_heartbeat": last_heartbeat},
        )
    return _check(
        "heartbeat",
        "Last heartbeat",
        tip,
        source,
        ok=False,
        detail=f"No. Last check-in was {_age_phrase(age)} ({stamp}), past the 180 second limit.",
        fix=FIX_HEARTBEAT,
        extra={"age_seconds": age, "last_heartbeat": last_heartbeat},
    )


def _last_hat_check(in_hat, last_hat, last_hat_time, tip_height, tip_time, found) -> dict:
    tip = "The newest indexed block that listed this name in the hat, next to the current tip."
    source = "lottery_active and blocks.time, compared with the indexed tip"
    tip_bit = f" Current tip is {tip_height}." if tip_height is not None else ""
    if tip_time is not None and tip_height is not None:
        tip_bit = f" Current tip is {tip_height} / {_fmt_time(tip_time)}."
    if last_hat is None:
        return _check(
            "last_hat",
            "Last hat block",
            tip,
            source,
            ok=False if found or tip_height is not None else None,
            detail="No hat block is indexed for this name." + tip_bit,
            fix=FIX_SYNC,
            extra={"last_hat": None, "tip_height": tip_height},
        )
    when = f" / {_fmt_time(last_hat_time)}" if last_hat_time is not None else ""
    behind = tip_height is not None and int(tip_height) - int(last_hat) >= 1
    if in_hat or not behind:
        return _check(
            "last_hat",
            "Last hat block",
            tip,
            source,
            ok=True,
            detail=f"Last in the hat at block {last_hat}{when}.{tip_bit}",
            extra={"last_hat": last_hat, "last_hat_time": last_hat_time, "tip_height": tip_height},
        )
    return _check(
        "last_hat",
        "Last hat block",
        tip,
        source,
        ok=False,
        detail=f"Out of hat since block {last_hat}{when}.{tip_bit}",
        fix=FIX_SYNC,
        extra={"last_hat": last_hat, "last_hat_time": last_hat_time, "tip_height": tip_height},
    )


def _verified_check(local_verified, attested, in_active, ever_in_hat, in_hat) -> dict:
    tip = (
        "A live blue check is shown when this node reports one. "
        "For every other name the node does not send that reading, so a past hat block is the strongest fact."
    )
    source = "getactivenodes.attested, or getlotteryinfo.local_x_verified for this node’s own wallet"
    if local_verified is True:
        return _check(
            "x_verified",
            "X verified",
            tip,
            source,
            ok=True,
            detail="Yes. This node’s own session reports a blue check for this name.",
        )
    if local_verified is False:
        return _check(
            "x_verified",
            "X verified",
            tip,
            source,
            ok=False,
            detail="No. This node’s own session does not report a blue check for this name.",
            fix=FIX_VERIFY,
        )
    if attested is True or (in_hat and in_active):
        return _check(
            "x_verified",
            "X verified",
            tip,
            source,
            ok=True,
            detail="Yes. A live check-in for this name is stamped, which is the live blue check this node can see.",
        )
    if attested is False:
        return _check(
            "x_verified",
            "X verified",
            tip,
            source,
            ok=False,
            detail="No. This name is checking in, and the seed has not stamped it as a live blue check.",
            fix=FIX_VERIFY,
        )
    if ever_in_hat:
        return _check(
            "x_verified",
            "X verified",
            tip,
            source,
            ok=None,
            detail=(
                "Live blue-check status is not available from this node. "
                "This name was in the hat before, so it had a stamp then."
            ),
        )
    return _check(
        "x_verified",
        "X verified",
        tip,
        source,
        ok=False,
        likely=True,
        detail="Likely no. This name has never been in the hat, and this node has no live blue-check reading for it.",
        fix=FIX_VERIFY,
    )


def _other_check(others: list[dict], live_ids: list[str]) -> dict:
    tip = "Another node or handle that lines up with this name. The chain has no person id, so a shared node or payout address is marked likely."
    source = "shared node id or payout address in the index, plus getactivenodes"
    if len(live_ids) > 1:
        shown = ", ".join(_short_id(n) for n in live_ids[:4])
        return _check(
            "other_node",
            "Another node",
            tip,
            source,
            ok=False,
            likely=False,
            detail=f"Yes. More than one node is checking in for this name right now ({shown}).",
            fix=FIX_OTHER,
            extra={"others": others, "live_node_ids": live_ids},
        )
    if not others:
        return _check(
            "other_node",
            "Another node",
            tip,
            source,
            ok=True,
            detail="No second node or handle is tied to this name in the data this explorer can see.",
            extra={"others": []},
        )
    bits = []
    for row in others:
        how = "the same node id" if row.get("via") == "node_id" else "the same payout address"
        where = _short_id(row.get("node_id")) if row.get("node_id") else (row.get("address") or "")
        hat = " That handle is in the hat." if row.get("in_hat") else ""
        bits.append(f"@{row['handle']} ({how} {where}).{hat}")
    return _check(
        "other_node",
        "Another node",
        tip,
        source,
        ok=False,
        likely=True,
        detail="Likely a second wallet. " + " ".join(bits),
        fix=FIX_OTHER,
        extra={"others": others},
    )


def _node_check(registered: str | None, live_ids: list[str], seen_node_ids: list[str]) -> dict:
    tip = "The node id stored when this name was claimed."
    source = "identities.node_id"
    if registered:
        extra = ""
        if live_ids and registered.lower() not in {n.lower() for n in live_ids}:
            extra = f" A live check-in is node {_short_id(live_ids[0])}."
        return _check(
            "registered_node",
            "Registered node",
            tip,
            source,
            ok=True,
            detail=f"Registered node id {registered}.{extra}",
            extra={"registered_node_id": registered},
        )
    if seen_node_ids:
        return _check(
            "registered_node",
            "Registered node",
            tip,
            source,
            ok=False,
            detail=f"No registered node id. The latest node seen for this name is {seen_node_ids[0]}.",
            fix=FIX_NODE,
            extra={"registered_node_id": None},
        )
    return _check(
        "registered_node",
        "Registered node",
        tip,
        source,
        ok=False,
        detail="No registered node id is stored for this name.",
        fix=FIX_NODE,
        extra={"registered_node_id": None},
    )


def _summary(body: dict, checks: list[dict]) -> str:
    handle = body.get("handle")
    if not handle:
        return "No @handle or address matched that search."
    also = body.get("also") or []
    prefix = ""
    if also:
        names = ", ".join(f"@{row.get('handle')}" for row in also if row.get("handle") and row.get("handle") != handle)
        if names:
            prefix = f"Showing @{handle}. Also linked: {names}. "
    if body.get("in_hat"):
        return prefix + f"@{handle} is in the hat."
    failed = next((c for c in checks if c.get("ok") is False and c.get("likely")), None)
    if failed is None:
        failed = next((c for c in checks if c.get("ok") is False), None)
    if failed is None:
        return prefix + f"@{handle} is not in the hat."
    return prefix + failed["detail"]
