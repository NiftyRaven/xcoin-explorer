"""Script, lottery, identity, and Ravencoin-style asset decoding."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

from explorer.chain import OP_RVN_ASSET, RVN_PREFIX, XHB1, XID1, XVA1, classify_asset_name

BASE58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hash256(data: bytes) -> bytes:
    return sha256(sha256(data))


def hash160(data: bytes) -> bytes:
    return hashlib.new("ripemd160", sha256(data)).digest()


def rpc_hex(raw: bytes) -> str:
    """Match Bitcoin/Raven GetHex(): little-endian blob printed reversed."""
    return raw[::-1].hex()


def rpc_hex_to_internal(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr)[::-1]


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = bytearray()
    while n > 0:
        n, r = divmod(n, 58)
        out.append(BASE58_ALPHABET[r])
    pad = 0
    for b in raw:
        if b == 0:
            pad += 1
        else:
            break
    return (BASE58_ALPHABET[0:1] * pad + out[::-1]).decode("ascii")


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s.encode("ascii"):
        n *= 58
        idx = BASE58_ALPHABET.find(bytes([ch]))
        if idx < 0:
            raise ValueError("invalid base58")
        n += idx
    raw = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big") if n else b""
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw.lstrip(b"\x00")


def encode_address(payload20: bytes, version: int) -> str:
    body = bytes([version]) + payload20
    return b58encode(body + hash256(body)[:4])


def decode_address(addr: str) -> tuple[int, bytes]:
    raw = b58decode(addr)
    if len(raw) < 5:
        raise ValueError("address too short")
    body, checksum = raw[:-4], raw[-4:]
    if hash256(body)[:4] != checksum:
        raise ValueError("bad address checksum")
    return body[0], body[1:]


def looks_like_txid(q: str) -> bool:
    q = q.lower().strip()
    return len(q) == 64 and all(c in "0123456789abcdef" for c in q)


def looks_like_height(q: str) -> bool:
    return q.isdigit() and 0 <= int(q) < 100_000_000


# --- Bitcoin script iterator -------------------------------------------------


def iter_script(script: bytes):
    i = 0
    n = len(script)
    while i < n:
        op = script[i]
        i += 1
        if op <= 75:
            data = script[i : i + op]
            i += op
            yield op, data
        elif op == 76:
            if i >= n:
                break
            ln = script[i]
            i += 1
            data = script[i : i + ln]
            i += ln
            yield op, data
        elif op == 77:
            if i + 1 >= n:
                break
            ln = int.from_bytes(script[i : i + 2], "little")
            i += 2
            data = script[i : i + ln]
            i += ln
            yield op, data
        else:
            yield op, b""


def pushdata_payloads(script: bytes) -> list[bytes]:
    return [data for op, data in iter_script(script) if data]


def is_op_return(script: bytes) -> bool:
    return bool(script) and script[0] == 0x6A


def is_p2pkh(script: bytes) -> bool:
    return (
        len(script) == 25
        and script[0] == 0x76
        and script[1] == 0xA9
        and script[2] == 0x14
        and script[23] == 0x88
        and script[24] == 0xAC
    )


def is_p2sh(script: bytes) -> bool:
    return len(script) == 23 and script[0] == 0xA9 and script[1] == 0x14 and script[22] == 0x87


def p2pkh_hash(script: bytes) -> bytes | None:
    if is_p2pkh(script):
        return script[3:23]
    # destination + OP_RVN_ASSET
    if len(script) > 25 and is_p2pkh(script[:25]):
        return script[3:23]
    return None


def split_asset_script(script: bytes) -> tuple[bytes, bytes | None]:
    """Return (destination_script, asset_payload_or_None)."""
    if not script:
        return script, None
    try:
        idx = script.index(bytes([OP_RVN_ASSET]))
    except ValueError:
        return script, None
    dest = script[:idx]
    rest = script[idx + 1 :]
    payloads = pushdata_payloads(rest) if rest else []
    payload = payloads[0] if payloads else rest
    return dest, payload


def address_from_script(script: bytes, network: str = "main") -> str | None:
    dest, _ = split_asset_script(script)
    if network == "main":
        pub_ver, script_ver = 76, 139
    else:
        pub_ver, script_ver = 140, 200
    if is_p2pkh(dest):
        return encode_address(dest[3:23], pub_ver)
    if is_p2sh(dest):
        return encode_address(dest[2:22], script_ver)
    # pubkey
    if len(dest) in (67, 35) and dest[-1] == 0xAC:
        pub = dest[1:-1] if dest[0] in (33, 65) else dest[:-1]
        if dest[0] in (33, 65) and len(dest) == dest[0] + 2:
            return encode_address(hash160(pub), pub_ver)
    return None


# --- OP_RETURN XHB1 / XID1 ---------------------------------------------------


def op_return_data(script: bytes) -> bytes | None:
    if not is_op_return(script):
        return None
    payloads = pushdata_payloads(script[1:]) if len(script) > 1 else []
    if payloads:
        return payloads[0]
    # bare 6a <data> without push (shouldn't happen)
    return script[1:] if len(script) > 1 else b""


@dataclass
class LotteryCommitment:
    active_ids: list[str] = field(default_factory=list)


def parse_xhb1(script: bytes) -> LotteryCommitment | None:
    data = op_return_data(script)
    if not data or len(data) < 8:
        return None
    if data[:4] != XHB1:
        return None
    n = int.from_bytes(data[4:8], "little")
    if n > 1024:
        return None
    if len(data) != 8 + n * 20:
        return None
    ids = [rpc_hex(data[8 + i * 20 : 28 + i * 20]) for i in range(n)]
    return LotteryCommitment(active_ids=ids)


@dataclass
class XvaStamp:
    handle: str
    node_id: str  # RPC GetHex of the 20-byte payout id
    stamp: str  # 65-byte compact sig, hex


def parse_xva1(script: bytes) -> list[XvaStamp] | None:
    """Coinbase OP_RETURN XVA1: count of (handle + id + stamp).

    Wire (see x-coin src/lottery.cpp MakeXvaCommitment):
      'XVA1' || uint32 LE n ||  n × { u8 hlen || handle || 20-byte id || 65-byte sig }
    """
    data = op_return_data(script)
    if not data or len(data) < 8:
        return None
    if data[:4] != XVA1:
        return None
    n = int.from_bytes(data[4:8], "little")
    if n > 1024:
        return None
    off = 8
    out: list[XvaStamp] = []
    for _ in range(n):
        if off >= len(data):
            return None
        hlen = data[off]
        off += 1
        if hlen < 1 or hlen > 32 or off + hlen + 20 + 65 > len(data):
            return None
        try:
            handle = data[off : off + hlen].decode("ascii").lower().lstrip("@")
        except UnicodeDecodeError:
            return None
        off += hlen
        if not handle or any(
            c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in handle
        ):
            return None
        node_id = rpc_hex(data[off : off + 20])
        off += 20
        stamp = data[off : off + 65].hex()
        off += 65
        out.append(XvaStamp(handle=handle, node_id=node_id, stamp=stamp))
    if off != len(data):
        return None
    return out


def parse_xid1(script: bytes) -> str | None:
    data = op_return_data(script)
    if not data or len(data) < 5:
        return None
    if data[:4] != XID1:
        return None
    handle = data[4:].decode("ascii", errors="replace").strip()
    if not handle or len(handle) > 32:
        return None
    return handle.lower().lstrip("@")


def _vout_atoms(value) -> int:
    """RPC coin amounts are XFER floats; ints are treated as already-atoms."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    from explorer.amounts import xfer_to_atoms

    return xfer_to_atoms(value)


def coinbase_lottery(vouts: list[dict], network: str = "main") -> dict:
    """XVA1 hat + value>0 payees. Winner is who got paid, matched by Hash160(script).

    XID1 is the asset-root claim — never lottery identity.
    """
    id_to_handle: dict[str, str] = {}
    xva: list[dict] = []
    xhb1: list[str] = []
    xid1 = None
    parsed_rows: list[tuple[dict, dict, str, dict]] = []

    for vout in vouts or []:
        spk = vout.get("scriptPubKey") or {}
        hexscript = spk.get("hex") or ""
        parsed = parse_vout_script(hexscript, network) if hexscript else {}
        parsed_rows.append((vout, spk, hexscript, parsed))
        if parsed.get("xid1"):
            xid1 = parsed["xid1"]
        if parsed.get("xhb1"):
            xhb1 = [str(i).lower() for i in parsed["xhb1"]]
        if parsed.get("xva1"):
            for row in parsed["xva1"]:
                nid = (row.get("node_id") or "").lower()
                handle = (row.get("handle") or "").lower().lstrip("@")
                if nid and handle:
                    id_to_handle[nid] = handle
                    xva.append({"handle": handle, "node_id": nid})

    winners: list[dict] = []
    rank = 0
    for vout, spk, hexscript, parsed in parsed_rows:
        atoms = _vout_atoms(vout.get("value"))
        if atoms <= 0:
            continue
        st = (spk.get("type") or parsed.get("script_type") or "")
        if st in ("nulldata", "nonstandard") or parsed.get("script_type") == "nulldata":
            continue
        if parsed.get("xhb1") or parsed.get("xva1") or parsed.get("xid1"):
            continue
        if hexscript:
            try:
                if is_op_return(bytes.fromhex(hexscript)):
                    continue
            except ValueError:
                continue
        node_id = lottery_node_id_from_script_hex(hexscript) if hexscript else None
        handle = id_to_handle.get((node_id or "").lower()) if node_id else None
        addresses = spk.get("addresses") or []
        address = addresses[0] if addresses else parsed.get("address")
        winners.append(
            {
                "rank": rank,
                "node_id": node_id,
                "address": address,
                "amount": atoms,
                "xaccount": handle,
                "is_producer": 1 if rank == 0 else 0,
            }
        )
        rank += 1

    handles: list[str] = []
    seen: set[str] = set()
    for row in xva:
        h = row["handle"]
        if h and h not in seen:
            seen.add(h)
            handles.append(h)

    winner_handle = winners[0]["xaccount"] if winners else None
    return {
        "xva": xva,
        "xhb1": xhb1,
        "id_to_handle": id_to_handle,
        "winners": winners,
        "handles": handles,
        "winner_handle": winner_handle,
        "xid1": xid1,
    }


# --- Asset payload (RIP-2) ---------------------------------------------------


@dataclass
class AssetOut:
    kind: str  # new, transfer, reissue, owner
    name: str
    amount: int = 0
    units: int | None = None
    reissuable: bool | None = None
    ipfs: str = ""
    type_name: str = ""


def _read_cstring(buf: bytes, i: int) -> tuple[str, int]:
    """Asset names are serialized as a compact-size string in Raven scripts.

    In practice the payload after 'rvnX' is: name bytes until we can parse
    remaining fixed fields. Raven uses a serialized CNewAsset:

        rvn + typebyte + name (compact size string) + int64 amount + ...

    Compact size: < 253 as a single byte length.
    """
    if i >= len(buf):
        raise ValueError("eof")
    ln = buf[i]
    i += 1
    if ln < 253:
        n = ln
    elif ln == 253:
        n = int.from_bytes(buf[i : i + 2], "little")
        i += 2
    else:
        raise ValueError("name too long")
    name = buf[i : i + n].decode("ascii", errors="replace")
    return name, i + n


def parse_asset_payload(payload: bytes) -> AssetOut | None:
    if not payload or len(payload) < 5:
        return None
    if payload[:3] != RVN_PREFIX:
        # Some builds omit the rvn prefix and start with type
        return None
    type_ch = chr(payload[3])
    rest = payload[4:]
    kind_map = {
        "q": "transfer",
        "t": "transfer",
        "n": "new",
        "o": "owner",
        "r": "reissue",
    }
    kind = kind_map.get(type_ch)
    if not kind:
        return None
    try:
        name, i = _read_cstring(rest, 0)
    except (ValueError, IndexError, UnicodeDecodeError):
        return None
    if not name:
        return None
    amount = 0
    units = None
    reissuable = None
    ipfs = ""
    try:
        if kind in ("transfer", "new", "reissue") and i + 8 <= len(rest):
            amount = int.from_bytes(rest[i : i + 8], "little", signed=True)
            i += 8
        if kind in ("new", "reissue") and i < len(rest):
            units = rest[i]
            i += 1
        if kind in ("new", "reissue") and i < len(rest):
            reissuable = bool(rest[i])
            i += 1
        if kind == "new" and i < len(rest):
            has_ipfs = rest[i]
            i += 1
            if has_ipfs and i < len(rest):
                ipfs = rest[i:].hex()
        elif kind in ("transfer", "reissue") and i < len(rest) and rest[i:]:
            # optional IPFS / message
            extra = rest[i:]
            if extra and extra[0] not in (0,):
                ipfs = extra.hex()
    except Exception:
        pass
    if kind == "owner" and not name.endswith("!"):
        name = name if name.endswith("!") else name + "!"
        amount = 1 * 100_000_000
    return AssetOut(
        kind=kind,
        name=name,
        amount=amount,
        units=units,
        reissuable=reissuable,
        ipfs=ipfs,
        type_name=classify_asset_name(name[:-1] if name.endswith("!") else name),
    )


def parse_vout_script(script_hex: str, network: str = "main") -> dict:
    script = bytes.fromhex(script_hex) if script_hex else b""
    dest, asset_payload = split_asset_script(script)
    address = address_from_script(dest, network) if dest else None
    out: dict = {
        "address": address,
        "script_type": "nulldata" if is_op_return(script) else "script",
        "op_return": None,
        "xhb1": None,
        "xva1": None,
        "xid1": None,
        "asset": None,
        "node_id": rpc_hex(hash160(script)) if script else None,
        "dest_node_id": rpc_hex(hash160(dest)) if dest else None,
    }
    if is_op_return(script):
        out["script_type"] = "nulldata"
        data = op_return_data(script)
        out["op_return"] = data.hex() if data else ""
        xhb = parse_xhb1(script)
        if xhb:
            out["xhb1"] = xhb.active_ids
        xva = parse_xva1(script)
        if xva:
            out["xva1"] = [
                {"handle": s.handle, "node_id": s.node_id, "stamp": s.stamp} for s in xva
            ]
        xid = parse_xid1(script)
        if xid:
            out["xid1"] = xid
    if is_p2pkh(dest):
        out["script_type"] = "pubkeyhash"
    elif is_p2sh(dest):
        out["script_type"] = "scripthash"
    if asset_payload:
        asset = parse_asset_payload(asset_payload)
        if asset:
            out["asset"] = {
                "kind": asset.kind,
                "name": asset.name,
                "amount": asset.amount,
                "units": asset.units,
                "reissuable": asset.reissuable,
                "ipfs": asset.ipfs,
                "type_name": asset.type_name,
            }
            out["script_type"] = {
                "new": "new_asset",
                "transfer": "transfer_asset",
                "reissue": "reissue_asset",
                "owner": "new_asset",
            }.get(asset.kind, out["script_type"])
    return out


def lottery_node_id_from_script_hex(script_hex: str) -> str:
    script = bytes.fromhex(script_hex)
    return rpc_hex(hash160(script))


def seed_for_draw(prev_block_hash_rpc: str, slot: int) -> str:
    prev = rpc_hex_to_internal(prev_block_hash_rpc)
    slot_le = int(slot).to_bytes(8, "little", signed=False)
    return rpc_hex(sha256(prev + slot_le))


def stream_u64(seed_rpc_hex: str, counter: int) -> int:
    seed = rpc_hex_to_internal(seed_rpc_hex)
    ctr = int(counter).to_bytes(4, "little", signed=False)
    h = sha256(seed + ctr)
    return int.from_bytes(h[:8], "little")


def select_winners(sorted_active_rpc_hex: list[str], seed_rpc_hex: str, k: int) -> list[str]:
    pool = list(sorted_active_rpc_hex)
    if not pool or k <= 0:
        return []
    if k >= len(pool):
        return pool
    winners = []
    for i in range(k):
        r = stream_u64(seed_rpc_hex, i)
        remaining = len(pool) - i
        idx = (r % remaining) + i
        pool[i], pool[idx] = pool[idx], pool[i]
        winners.append(pool[i])
    return winners


def split_reward(total: int, winners: int) -> list[int]:
    if winners <= 0 or total <= 0:
        return []
    base = total // winners
    rem = total % winners
    parts = [base] * winners
    for i in range(rem):
        parts[i] += 1
    return parts


def classify_search(q: str) -> str:
    q = (q or "").strip()
    if not q:
        return "empty"
    if q.startswith("@"):
        return "handle"
    if looks_like_height(q):
        return "height"
    if looks_like_txid(q):
        return "hash"
    if len(q) == 40 and all(c in "0123456789abcdefABCDEF" for c in q):
        return "node_id"
    try:
        decode_address(q)
        return "address"
    except Exception:
        pass
    if any(ch in q for ch in "/#!"):
        return "asset"
    if len(q) >= 3 and q.upper() == q and q.replace("_", "").isalnum():
        return "asset"
    return "query"
