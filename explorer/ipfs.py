"""Resolve Ravencoin-style asset IPFS hashes for the explorer UI."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from typing import Any

import httpx

from explorer.decode import normalize_ipfs

CID_RE = re.compile(
    r"^(Qm[1-9A-HJ-NP-Za-km-z]{44}|baf[a-z2-7]{50,})$"
)

DEDICATED_GATEWAY = "https://xfer.mypinata.cloud/ipfs/"

GATEWAYS = (
    DEDICATED_GATEWAY,
    "https://gateway.pinata.cloud/ipfs/",
    "https://dweb.link/ipfs/",
    "https://w3s.link/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://ipfs.io/ipfs/",
)

# Dedicated Pinata returns this for CIDs never pinned to the xfer account.
# Those CIDs are still valid IPFS; try the next gateway / content proxy.
GATEWAY_MISS_MARKERS = (
    b"ERR_ID:00006",
    b"does not have this content pinned",
    b"the owner of this gateway does not have this content",
)

MAX_INSPECT_BYTES = 2 * 1024 * 1024
MAX_PEEK_BYTES = 64 * 1024
MAX_CONTENT_BYTES = 80 * 1024 * 1024
FETCH_TIMEOUT = 22.0
# Token JSON only. Pictures and video still use FETCH_TIMEOUT / MAX_CONTENT_BYTES.
JSON_MAX_BYTES = 256 * 1024
JSON_FETCH_TIMEOUT = 12.0
META_CACHE_TTL = 600
META_CACHE_MAX = 128
RAW_JSON_CAP = 32_000
TRAIT_CAP = 24

IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "image/avif",
    "image/bmp",
}
VIDEO_TYPES = {
    "video/mp4",
    "video/webm",
    "video/ogg",
    "video/quicktime",
}
AUDIO_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "audio/flac",
}


def valid_cid(cid: str) -> bool:
    return bool(cid and CID_RE.match(cid))


def gateway_urls(cid: str) -> list[str]:
    return [g + cid for g in GATEWAYS]


def pinata_view_url(cid: str) -> str:
    return f"{DEDICATED_GATEWAY}{cid}"


def ipfs_content_url(cid: str) -> str:
    return f"/api/ipfs/content/{cid}"


def _is_gateway_miss(content_type: str, raw: bytes) -> bool:
    """True when a gateway answered without the CID bytes (Pinata owner-not-pinned HTML/text)."""
    head = (raw or b"")[:8192].lower()
    if any(marker.lower() in head for marker in GATEWAY_MISS_MARKERS):
        return True
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("text/html", "text/plain") and b"err_id:" in head:
        return True
    return False


def attach_ipfs_fields(asset: dict, rpc_data: dict | None = None) -> dict:
    raw = asset.get("ipfs") or ""
    rpc_hash = ""
    has_flag = False
    if isinstance(rpc_data, dict):
        rpc_hash = rpc_data.get("ipfs_hash") or rpc_data.get("ipfs") or rpc_data.get("message") or ""
        has_flag = bool(rpc_data.get("has_ipfs"))
    cid = normalize_ipfs(rpc_hash) or normalize_ipfs(raw)
    asset["ipfs_cid"] = cid
    asset["has_ipfs"] = bool(cid or raw or has_flag)
    asset["ipfs_gateways"] = gateway_urls(cid) if cid else []
    return asset


def _sniff_kind(content_type: str, raw: bytes) -> tuple[str, str]:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in IMAGE_TYPES:
        return "image", ct
    if ct in VIDEO_TYPES:
        return "video", ct
    if ct in AUDIO_TYPES:
        return "audio", ct
    if ct in ("application/json", "text/json"):
        return "json", "application/json"
    if ct.startswith("text/"):
        head = raw.lstrip()[:1]
        if head in (b"{", b"["):
            return "json", "application/json"
        return "text", ct or "text/plain"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
        return "image", "image/gif"
    if raw[0:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image", "image/webp"
    if raw[4:8] == b"ftyp":
        return "video", "video/mp4"
    if raw.startswith(b"\x1aE\xdf\xa3"):
        return "video", "video/webm"
    if raw.startswith(b"ID3") or raw[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "audio", "audio/mpeg"
    trimmed = raw.lstrip()
    if trimmed.startswith(b"{") or trimmed.startswith(b"["):
        return "json", "application/json"
    if trimmed[:5].lower() in (b"<svg ", b"<svg\n", b"<svg\r", b"<svg\t"):
        return "image", "image/svg+xml"
    return "other", ct or "application/octet-stream"


def _media_ref(value: Any) -> dict[str, str] | None:
    if not value:
        return None
    if isinstance(value, dict):
        value = value.get("href") or value.get("url") or value.get("/") or ""
    s = str(value).strip()
    if not s:
        return None
    if s.startswith(("http://", "https://")):
        return {"kind": "url", "src": s, "cid": normalize_ipfs(s)}
    cid = normalize_ipfs(s)
    if cid:
        return {"kind": "ipfs", "src": ipfs_content_url(cid), "cid": cid}
    return None


_TAG_RE = re.compile(r"<[^>]*>", re.DOTALL)
_CONTRACTISH = re.compile(r"^contract([._-]|$)", re.IGNORECASE)
_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)

_CACHE_LOCK = threading.Lock()
_META_CACHE: dict[str, tuple[float, dict]] = {}


def clear_metadata_cache() -> None:
    with _CACHE_LOCK:
        _META_CACHE.clear()


def _cache_get(cid: str) -> dict | None:
    with _CACHE_LOCK:
        hit = _META_CACHE.get(cid)
        if not hit:
            return None
        ts, value = hit
        if time.time() - ts > META_CACHE_TTL:
            _META_CACHE.pop(cid, None)
            return None
        return json.loads(json.dumps(value))


def _cache_put(cid: str, value: dict) -> None:
    with _CACHE_LOCK:
        if cid not in _META_CACHE and len(_META_CACHE) >= META_CACHE_MAX:
            oldest = min(_META_CACHE, key=lambda key: _META_CACHE[key][0])
            _META_CACHE.pop(oldest, None)
        _META_CACHE[cid] = (time.time(), json.loads(json.dumps(value)))


def plain_text(value: Any, limit: int, *, keep_breaks: bool = False) -> str:
    """Tags become plain text. Nothing here is HTML."""
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return ""
    text = _TAG_RE.sub("", text).replace("\x00", "")
    if keep_breaks:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def safe_http_url(value: Any, limit: int = 200) -> str:
    """http and https only. No login info in the URL."""
    text = plain_text(value, limit)
    if not text or not _HTTP_RE.match(text):
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    if parsed.username or parsed.password or not parsed.hostname:
        return ""
    return text


def read_traits(meta: dict, cap: int = TRAIT_CAP) -> list[dict[str, str]]:
    raw = meta.get("attributes") if isinstance(meta, dict) else None
    if not isinstance(raw, list):
        raw = meta.get("traits") if isinstance(meta, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for row in raw:
        if len(out) >= cap:
            break
        if not isinstance(row, dict):
            continue
        trait = plain_text(row.get("trait_type") or row.get("trait") or row.get("type") or "", 40)
        value = row.get("value")
        if value is None:
            value = row.get("val")
        shown = plain_text(value, 80)
        if trait and shown:
            out.append({"trait_type": trait, "value": shown})
    return out


def _clean_json(value: Any, depth: int = 0) -> Any:
    """Drop contract-like keys and cap nesting. Strings stay data, not markup."""
    if depth > 6:
        return None
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_clean_json(item, depth + 1) for item in value[:TRAIT_CAP]]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            if not isinstance(key, str) or _CONTRACTISH.match(key):
                continue
            nxt = _clean_json(item, depth + 1)
            if nxt is not None:
                out[key] = nxt
        return out
    return None


def public_json_text(body: Any) -> str:
    cleaned = _clean_json(body, 0)
    if not isinstance(cleaned, (dict, list)):
        return ""
    try:
        text = json.dumps(cleaned, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    if not text or text in ("{}", "[]"):
        return ""
    if len(text) > RAW_JSON_CAP:
        return text[:RAW_JSON_CAP] + "\n…"
    return text


def metadata_view(nft: dict) -> dict[str, Any] | None:
    if not isinstance(nft, dict):
        return None
    image = nft.get("image") if isinstance(nft.get("image"), dict) else None
    attrs = nft.get("attributes") if isinstance(nft.get("attributes"), list) else []
    name = nft.get("name") or ""
    description = nft.get("description") or ""
    external = nft.get("external_url") or ""
    if not any((name, description, external, image, attrs)):
        return None
    view: dict[str, Any] = {
        "name": name,
        "description": description,
        "external_url": external,
        "attributes": attrs,
    }
    if image:
        view["image"] = image
    return view


def extract_nft_media(meta: dict) -> dict[str, Any]:
    image = None
    # xcoin-asset-1: image_url first (same as Launch / PR #60), then icon / image.
    for key in ("image_url", "icon", "image", "img", "imageUrl", "thumbnail", "preview"):
        image = _media_ref(meta.get(key))
        if image:
            break
    animation = None
    for key in ("animation_url", "animationUrl", "video", "video_url", "animation"):
        animation = _media_ref(meta.get(key))
        if animation:
            break
    audio = None
    for key in ("audio", "audio_url", "audioUrl"):
        audio = _media_ref(meta.get(key))
        if audio:
            break
    return {
        "name": plain_text(meta.get("name") or meta.get("title") or "", 80),
        "description": plain_text(meta.get("description") or meta.get("text") or "", 2000, keep_breaks=True),
        "external_url": (
            safe_http_url(meta.get("external_url") or "")
            or safe_http_url(meta.get("externalUrl") or "")
            or safe_http_url(meta.get("website_url") or "")
        ),
        "image": image,
        "animation": animation,
        "audio": audio,
        "attributes": read_traits(meta),
    }


def _fetch(cid: str, limit: int, peek: bool = False) -> tuple[bytes, str, str, bool]:
    if not valid_cid(cid):
        raise ValueError("invalid IPFS CID")
    last_err = "unreachable"
    headers = {"User-Agent": "XFER-Explorer/1.0"}
    # Peek is the metadata/sniff path: shorter wait, and JSON stops at 256 KB.
    timeout = JSON_FETCH_TIMEOUT if peek else FETCH_TIMEOUT
    for base in GATEWAYS:
        url = base + cid
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
                with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        last_err = f"{resp.status_code} from {base}"
                        continue
                    ct = resp.headers.get("content-type") or ""
                    chunks: list[bytes] = []
                    total = 0
                    truncated = False
                    missed = False
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        chunks.append(chunk)
                        raw = b"".join(chunks)
                        if _is_gateway_miss(ct, raw):
                            last_err = f"unpinned/miss from {base}"
                            missed = True
                            break
                        if peek:
                            kind, _sniffed = _sniff_kind(ct, raw[:MAX_PEEK_BYTES])
                            if kind in ("image", "video", "audio") and total >= min(4096, MAX_PEEK_BYTES):
                                truncated = True
                                return raw[:MAX_PEEK_BYTES], ct, url, truncated
                            if kind in ("json", "text") and total > JSON_MAX_BYTES:
                                truncated = True
                                return raw[:JSON_MAX_BYTES], ct, url, truncated
                            if kind not in ("json", "text") and total > MAX_PEEK_BYTES:
                                truncated = True
                                return raw[:MAX_PEEK_BYTES], ct, url, truncated
                        elif total > limit:
                            raise ValueError("IPFS object exceeds explorer size limit")
                    if missed:
                        continue
                    return b"".join(chunks), ct, url, False
        except httpx.HTTPError as e:
            last_err = str(e)
            continue
    raise ValueError(f"IPFS fetch failed: {last_err}")


def _inspect_uncached(cid: str) -> dict[str, Any]:
    raw, content_type, source, truncated = _fetch(cid, JSON_MAX_BYTES, peek=True)
    kind, sniffed = _sniff_kind(content_type, raw)
    out: dict[str, Any] = {
        "cid": cid,
        "kind": kind,
        "content_type": sniffed,
        "size": len(raw),
        "truncated": truncated,
        "source": source,
        "url": ipfs_content_url(cid),
        "gateways": gateway_urls(cid),
    }
    if kind == "json" and not truncated:
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Bad JSON is not an error page. The asset view just skips the card.
            return out
        cleaned = _clean_json(meta)
        if cleaned is not None:
            out["metadata"] = cleaned
        if isinstance(meta, dict):
            nft = extract_nft_media(meta)
            out["nft"] = nft
            view = metadata_view(nft)
            if view:
                out["view"] = view
        raw_json = public_json_text(meta)
        if raw_json:
            out["raw_json"] = raw_json
    elif kind == "text":
        out["text"] = raw.decode("utf-8", errors="replace")[:8000]
    return out


def inspect_cid(cid: str) -> dict[str, Any]:
    cid = normalize_ipfs(cid)
    if not valid_cid(cid):
        raise ValueError("invalid IPFS CID")
    cached = _cache_get(cid)
    if cached is not None:
        return cached
    out = _inspect_uncached(cid)
    _cache_put(cid, out)
    return _cache_get(cid) or out


def fetch_content(cid: str) -> tuple[bytes, str]:
    cid = normalize_ipfs(cid)
    raw, content_type, _source, _truncated = _fetch(cid, MAX_CONTENT_BYTES, peek=False)
    _kind, sniffed = _sniff_kind(content_type, raw)
    return raw, sniffed
