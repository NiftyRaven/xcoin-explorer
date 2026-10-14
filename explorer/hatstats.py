"""Per-handle hat stats.

A handle can run several nodes over time. The identity row keeps only the
first node id, so these stats follow every node id tied to the handle and
add those draws together. A row is counted when its handle matches, or when
the node id is one of theirs and the row has no handle of its own. A row
that names a different handle is left with that handle.
"""

from __future__ import annotations

import math
import time
from typing import Any

DAY_SECONDS = 86400
RECENT_DRAWS = 20

_HANDLE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")

REASON_TEXT = {
    "no_heartbeat": (
        "No heartbeat. This name’s app has not checked in during the last 3 minutes, "
        "so it is not in this draw."
    ),
    "not_verified": (
        "Not verified. Only an account with a check mark on X can join the draw."
    ),
    "attestation": (
        "Attestation. A heartbeat is coming in, but this name is not confirmed for the draw yet."
    ),
    "recently_restarted": (
        "Recently restarted. This name checked in after this minute’s draw was set, "
        "so it can join the next one."
    ),
}

# Snap a rare result to a round "1 in N" a person can read at a glance.
_FRIENDLY_ODDS = (2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 75, 100, 200, 500, 1000)


def valid_handle(handle: str | None) -> bool:
    name = handle or ""
    return bool(name) and all(c in _HANDLE_CHARS for c in name)


def absence_reason(
    *,
    in_hat: bool,
    heartbeat: bool,
    attested: bool | None,
    ever_in_hat: bool,
    attestation_required: bool | None,
    local_verified: bool | None,
) -> str | None:
    """Why a name is sitting out this minute, or None when it is in the draw."""
    if in_hat:
        return None
    if local_verified is False:
        return "not_verified"
    if heartbeat and attested is False:
        return "attestation"
    if heartbeat and attested is True:
        return "recently_restarted"
    if heartbeat and attested is None:
        if attestation_required:
            return "attestation"
        return "recently_restarted"
    if not ever_in_hat:
        return "not_verified"
    return "no_heartbeat"


def luck_note(wins: int, expected: float, variance: float) -> dict[str, Any]:
    """Compare actual wins with the fair share. Draws are treated as random."""
    if expected <= 0:
        return {
            "tone": "none",
            "one_in": None,
            "text": "No draws yet, so there is nothing to compare. Each draw is random.",
        }
    sigma = math.sqrt(variance) if variance > 1e-12 else 0.0
    if sigma < 1e-9:
        return {
            "tone": "average",
            "one_in": None,
            "text": "This matches the fair share. Each draw is random.",
        }
    z = (wins - expected) / sigma
    if abs(z) < 0.6:
        return {
            "tone": "average",
            "one_in": None,
            "text": "About average luck. Wins are close to the fair share. Each draw is random.",
        }
    if wins < expected:
        # Chance of this many wins or fewer.
        tail = _phi((wins + 0.5 - expected) / sigma)
        one_in = _about_one_in(tail)
        odds = _odds_sentence(one_in, "low")
        return {
            "tone": "below",
            "one_in": one_in,
            "text": f"Below average luck. {odds} Each draw is random.",
        }
    tail = 1.0 - _phi((wins - 0.5 - expected) / sigma)
    one_in = _about_one_in(tail)
    odds = _odds_sentence(one_in, "high")
    return {
        "tone": "above",
        "one_in": one_in,
        "text": f"Above average luck. {odds} Each draw is random.",
    }


def compute_hat_stats(db, handle: str, *, now: int | None = None, recent: int = RECENT_DRAWS) -> dict | None:
    """Historical hat numbers for one handle. Live in-hat state is added by the API."""
    if not valid_handle(handle):
        return None
    if now is None:
        now = int(time.time())
    recent = max(1, min(int(recent or RECENT_DRAWS), 50))
    conn = db.conn
    ids = _node_ids(conn, handle)
    included = _included_heights(conn, handle, ids)
    wins = _win_rows(conn, handle, ids)
    seen = _node_seen(conn, handle, ids)
    for row in seen:
        nid = (row["node_id"] or "").lower()
        if nid:
            ids.add(nid)

    times = _block_times(conn, included)
    cutoff = int(now) - DAY_SECONDS
    draws_24h = sum(1 for h in included if times.get(h) is not None and int(times[h]) >= cutoff)
    draws_24h_total = int(
        _cell(
            conn.execute(
                "SELECT COUNT(*) AS c FROM blocks WHERE height>=1 AND time>=?",
                (cutoff,),
            ).fetchone()
        )
    )
    first = min(included) if included else None
    last = max(included) if included else None
    if first is None:
        draws_since = 0
        included_percent = None
    else:
        draws_since = int(
            _cell(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM blocks WHERE height>=1 AND height>=?",
                    (first,),
                ).fetchone()
            )
        )
        included_percent = (
            float(f"{(100.0 * len(included) / draws_since):.1f}") if draws_since else None
        )

    sizes = _hat_sizes(conn, included)
    expected = 0.0
    variance = 0.0
    for height in included:
        n = sizes.get(height) or 0
        if n <= 0:
            continue
        p = 1.0 / n
        expected += p
        variance += p * (1.0 - p)

    earned = 0
    win_heights: set[int] = set()
    wins_by_node: dict[str, int] = {}
    addresses: dict[str, dict[str, Any]] = {}
    for row in wins:
        earned += int(row["amount"] or 0)
        height = int(row["height"])
        win_heights.add(height)
        nid = (row["node_id"] or "").lower()
        if nid:
            wins_by_node[nid] = wins_by_node.get(nid, 0) + 1
            ids.add(nid)
        addr = row["address"] or ""
        if addr:
            bucket = addresses.setdefault(addr, {"node_ids": set(), "wins": 0})
            bucket["wins"] += 1
            if nid:
                bucket["node_ids"].add(nid)

    draws_by_node, span_by_node = _draws_by_node(conn, handle, ids)
    for nid, span in span_by_node.items():
        ids.add(nid)

    last_seen: int | None = None
    seen_by_id: dict[str, dict] = {}
    for row in seen:
        nid = (row["node_id"] or "").lower()
        if not nid:
            continue
        seen_by_id[nid] = {"address": row["address"], "last_seen": row["last_seen"]}
        if row["last_seen"] is not None:
            stamp = int(row["last_seen"])
            if last_seen is None or stamp > last_seen:
                last_seen = stamp
        addr = row["address"] or ""
        if addr:
            bucket = addresses.setdefault(addr, {"node_ids": set(), "wins": 0})
            bucket["node_ids"].add(nid)

    nodes = []
    for nid in ids:
        span = span_by_node.get(nid) or {}
        row = seen_by_id.get(nid) or {}
        node_addresses = sorted(
            {a for a, info in addresses.items() if nid in info["node_ids"]}
        )
        nodes.append(
            {
                "node_id": nid,
                "addresses": node_addresses,
                "draws": len(draws_by_node.get(nid) or ()),
                "wins": wins_by_node.get(nid, 0),
                "first_height": span.get("first"),
                "last_height": span.get("last"),
                "last_seen": int(row["last_seen"]) if row.get("last_seen") is not None else None,
            }
        )
    nodes.sort(key=lambda n: (-(n["last_height"] or -1), n["node_id"]))

    payouts = [
        {
            "address": addr,
            "node_ids": sorted(info["node_ids"]),
            "wins": info["wins"],
        }
        for addr, info in addresses.items()
    ]
    payouts.sort(key=lambda r: (-r["wins"], r["address"]))

    recent_draws = _recent_draws(conn, included, win_heights, recent)
    luck = luck_note(len(wins), expected, variance)
    return {
        "handle": handle,
        "last_heartbeat": last_seen,
        "draws_24h": draws_24h,
        "draws_24h_total": draws_24h_total,
        "draws_all": len(included),
        "draws_since_joined": draws_since,
        "included_percent": included_percent,
        "first_draw": first,
        "last_draw": last,
        "wins": len(wins),
        "earned": earned,
        "expected_wins": round(expected, 4),
        "luck": luck,
        "recent_draws": recent_draws,
        "nodes": nodes,
        "payout_addresses": payouts,
    }


def attach_live_status(
    hist: dict,
    *,
    hat_handles: set[str],
    heartbeat_handles: set[str],
    nodes: list[dict],
    live: dict | None,
) -> dict:
    """Add this-minute hat state. Does not change the cached history."""
    handle = hist["handle"]
    our_ids = {n.get("node_id") or "" for n in hist.get("nodes") or []}
    mine = [
        n
        for n in nodes
        if n.get("handle") == handle or (not n.get("handle") and n.get("id") in our_ids)
    ]
    in_hat = handle in hat_handles
    heartbeat = handle in heartbeat_handles or bool(mine)
    flags = [n.get("attested") for n in mine if isinstance(n.get("attested"), bool)]
    if any(flags):
        attested: bool | None = True
    elif flags:
        attested = False
    else:
        attested = None
    local_verified = None
    attestation_required = None
    if isinstance(live, dict):
        local_handle = (live.get("local_xaccount") or "").strip().lower().lstrip("@")
        if local_handle and local_handle == handle and isinstance(live.get("local_x_verified"), bool):
            local_verified = bool(live.get("local_x_verified"))
        if isinstance(live.get("attestation_required"), bool):
            attestation_required = bool(live.get("attestation_required"))
    reason = absence_reason(
        in_hat=in_hat,
        heartbeat=heartbeat,
        attested=attested,
        ever_in_hat=bool(hist.get("draws_all") or hist.get("wins")),
        attestation_required=attestation_required,
        local_verified=local_verified,
    )
    live_seen = [int(n["lastseen"]) for n in mine if isinstance(n.get("lastseen"), int)]
    stamps = [s for s in [hist.get("last_heartbeat"), *live_seen] if isinstance(s, int)]
    # Copy so a live node never rewrites the cached history.
    body = dict(hist)
    listed = [dict(n) for n in hist.get("nodes") or []]
    by_id = {n["node_id"]: n for n in listed if n.get("node_id")}
    for node in mine:
        nid = node.get("id") or ""
        if not nid:
            continue
        stamp = node.get("lastseen") if isinstance(node.get("lastseen"), int) else None
        current = by_id.get(nid)
        if current is None:
            current = {
                "node_id": nid,
                "addresses": [],
                "draws": 0,
                "wins": 0,
                "first_height": None,
                "last_height": None,
                "last_seen": stamp,
            }
            listed.insert(0, current)
            by_id[nid] = current
        elif stamp is not None and (current.get("last_seen") is None or stamp > current["last_seen"]):
            current["last_seen"] = stamp
    body["nodes"] = listed
    body["in_hat"] = in_hat
    body["heartbeat"] = heartbeat
    body["reason"] = reason
    body["reason_text"] = REASON_TEXT.get(reason) if reason else None
    body["last_heartbeat"] = max(stamps) if stamps else None
    return body


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _about_one_in(tail: float) -> int | None:
    if tail <= 0 or tail >= 0.5:
        return None
    raw = 1.0 / tail
    if raw > _FRIENDLY_ODDS[-1]:
        return _FRIENDLY_ODDS[-1]
    return min(_FRIENDLY_ODDS, key=lambda n: abs(math.log(n) - math.log(raw)))


def _odds_sentence(one_in: int | None, side: str) -> str:
    where = "this low" if side == "low" else "this high"
    if one_in is None:
        return "This is a common result."
    if one_in >= _FRIENDLY_ODDS[-1]:
        return (
            f"Fewer than 1 in {one_in:,} players with the same chances see a result {where}."
        )
    return f"About 1 in {one_in} players with the same chances see a result {where}."


def _cell(row, default=0):
    if row is None:
        return default
    try:
        value = row["c"]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def _chunks(items, size=400):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _node_ids(conn, handle: str) -> set[str]:
    ids: set[str] = set()
    queries = (
        (
            """
            SELECT node_id FROM lottery_active
            WHERE node_id IS NOT NULL AND TRIM(node_id)!='' AND lower(TRIM(xaccount))=?
            """,
            (handle,),
        ),
        (
            """
            SELECT node_id FROM lottery_wins
            WHERE node_id IS NOT NULL AND TRIM(node_id)!='' AND lower(TRIM(xaccount))=?
            """,
            (handle,),
        ),
        (
            """
            SELECT node_id FROM node_seen
            WHERE node_id IS NOT NULL AND TRIM(node_id)!='' AND lower(TRIM(xaccount))=?
            """,
            (handle,),
        ),
        (
            """
            SELECT node_id FROM identities
            WHERE handle=? AND node_id IS NOT NULL AND TRIM(node_id)!=''
            """,
            (handle,),
        ),
    )
    for sql, params in queries:
        for row in conn.execute(sql, params).fetchall():
            ids.add(str(row["node_id"]).lower())
    return ids


def _included_heights(conn, handle: str, ids: set[str]) -> set[int]:
    heights: set[int] = set()
    for row in conn.execute(
        """
        SELECT DISTINCT height FROM lottery_active
        WHERE height>=1 AND lower(TRIM(xaccount))=?
        """,
        (handle,),
    ).fetchall():
        heights.add(int(row["height"]))
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"""
            SELECT DISTINCT height FROM lottery_active
            WHERE height>=1 AND lower(node_id) IN ({marks})
              AND (xaccount IS NULL OR TRIM(xaccount)='')
            """,
            tuple(chunk),
        ).fetchall():
            heights.add(int(row["height"]))
    return heights


def _win_rows(conn, handle: str, ids: set[str]) -> list:
    rows = list(
        conn.execute(
            """
            SELECT height, rank, node_id, address, amount FROM lottery_wins
            WHERE height>=1 AND lower(TRIM(xaccount))=?
            """,
            (handle,),
        ).fetchall()
    )
    seen = {(int(r["height"]), int(r["rank"])) for r in rows}
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"""
            SELECT height, rank, node_id, address, amount FROM lottery_wins
            WHERE height>=1 AND lower(node_id) IN ({marks})
              AND (xaccount IS NULL OR TRIM(xaccount)='')
            """,
            tuple(chunk),
        ).fetchall():
            key = (int(row["height"]), int(row["rank"]))
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return rows


def _node_seen(conn, handle: str, ids: set[str]) -> list:
    rows = list(
        conn.execute(
            """
            SELECT node_id, address, xaccount, last_seen FROM node_seen
            WHERE lower(TRIM(xaccount))=?
            """,
            (handle,),
        ).fetchall()
    )
    have = {(r["node_id"] or "").lower() for r in rows}
    missing = [nid for nid in ids if nid not in have]
    for chunk in _chunks(missing):
        marks = ",".join("?" * len(chunk))
        rows.extend(
            conn.execute(
                f"""
                SELECT node_id, address, xaccount, last_seen FROM node_seen
                WHERE lower(node_id) IN ({marks})
                """,
                tuple(chunk),
            ).fetchall()
        )
    return rows


def _block_times(conn, heights: set[int]) -> dict[int, int | None]:
    times: dict[int, int | None] = {}
    for chunk in _chunks(heights):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT height, time FROM blocks WHERE height IN ({marks})",
            tuple(chunk),
        ).fetchall():
            times[int(row["height"])] = None if row["time"] is None else int(row["time"])
    return times


def _hat_sizes(conn, heights: set[int]) -> dict[int, int]:
    sizes: dict[int, int] = {}
    for chunk in _chunks(heights):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT height, COUNT(*) AS n FROM lottery_active WHERE height IN ({marks}) GROUP BY height",
            tuple(chunk),
        ).fetchall():
            sizes[int(row["height"])] = int(row["n"] or 0)
    return sizes


def _draws_by_node(conn, handle: str, ids: set[str]) -> tuple[dict[str, set[int]], dict[str, dict]]:
    draws: dict[str, set[int]] = {}
    span: dict[str, dict] = {}

    def take(row, count: bool) -> None:
        nid = (row["node_id"] or "").lower()
        if not nid:
            return
        height = int(row["height"])
        if count:
            draws.setdefault(nid, set()).add(height)
            cur = span.setdefault(nid, {"first": height, "last": height})
            cur["first"] = min(cur["first"], height)
            cur["last"] = max(cur["last"], height)

    for row in conn.execute(
        """
        SELECT height, node_id FROM lottery_active
        WHERE height>=1 AND lower(TRIM(xaccount))=?
        """,
        (handle,),
    ).fetchall():
        take(row, True)
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"""
            SELECT height, node_id FROM lottery_active
            WHERE height>=1 AND lower(node_id) IN ({marks})
              AND (xaccount IS NULL OR TRIM(xaccount)='')
            """,
            tuple(chunk),
        ).fetchall():
            take(row, True)
    return draws, span


def _recent_draws(conn, included: set[int], win_heights: set[int], recent: int) -> list[dict]:
    blocks = conn.execute(
        """
        SELECT height, time, active_count FROM blocks
        WHERE height>=1
        ORDER BY height DESC
        LIMIT ?
        """,
        (recent,),
    ).fetchall()
    if not blocks:
        return []
    heights = [int(r["height"]) for r in blocks]
    marks = ",".join("?" * len(heights))
    sizes = {
        int(r["height"]): int(r["n"] or 0)
        for r in conn.execute(
            f"SELECT height, COUNT(*) AS n FROM lottery_active WHERE height IN ({marks}) GROUP BY height",
            tuple(heights),
        ).fetchall()
    }
    winners = {}
    for row in conn.execute(
        f"""
        SELECT height, xaccount FROM lottery_wins
        WHERE rank=0 AND height IN ({marks})
        """,
        tuple(heights),
    ).fetchall():
        name = (row["xaccount"] or "").strip().lower().lstrip("@")
        winners[int(row["height"])] = name or None
    out = []
    for row in blocks:
        height = int(row["height"])
        hat_size = sizes.get(height)
        if not hat_size:
            hat_size = int(row["active_count"] or 0)
        out.append(
            {
                "height": height,
                "time": None if row["time"] is None else int(row["time"]),
                "hat_size": hat_size,
                "in_hat": height in included,
                "winner": winners.get(height),
                "won": height in win_heights,
            }
        )
    return out
