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
DEDICATED_HOST = "xfer.mypinata.cloud"
# Launch already guards these. Pass the asset name; do not fetch arbitrary CIDs.
RELAY_ORIGIN = "https://xferchain.net"
RELAY_HOST = "xferchain.net"
RELAY_LAUNCH_IPFS = "https://xferchain.net/api/launch/ipfs/"
RELAY_ASSETS_IPFS = "https://xferchain.net/api/assets/ipfs/"

# Fetch path is our gateway only. Public gateways are not fallbacks.
GATEWAYS = (DEDICATED_GATEWAY,)

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
    return [pinata_view_url(cid)] if valid_cid(cid) else []


def pinata_view_url(cid: str) -> str:
    return f"{DEDICATED_GATEWAY}{cid}"


def ipfs_content_url(cid: str) -> str:
    return f"/api/ipfs/content/{cid}"


def safe_rel_path(value: Any) -> str:
    """A path inside one CID. No host, no .., no query."""
    if not isinstance(value, str):
        return ""
    text = value.strip().replace("\\", "/")
    if not text:
        return ""
    try:
        text = urllib.parse.unquote(text)
    except ValueError:
        return ""
    if text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    if not text or any(mark in text for mark in ("?", "#")):
        return ""
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or len(parts) > 8:
        return ""
    if any(part == ".." or len(part) > 180 or re.search(r"[\x00-\x1f\x7f:]", part) for part in parts):
        return ""
    joined = "/".join(parts)
    if len(joined) > 300:
        return ""
    return joined


def gateway_file_url(cid: str, path: str = "") -> str:
    url = f"{DEDICATED_GATEWAY}{cid}"
    rel = safe_rel_path(path)
    if rel:
        url += "/" + "/".join(urllib.parse.quote(part) for part in rel.split("/"))
    return url


def relay_media_url(cid: str, path: str = "", name: str = "") -> str:
    rel = safe_rel_path(path)
    query: dict[str, str] = {}
    if rel:
        query["path"] = rel
    clean = _clean_asset_name(name)
    if clean:
        query["name"] = clean
    qs = urllib.parse.urlencode(query)
    base = f"{RELAY_ASSETS_IPFS}media/{urllib.parse.quote(cid, safe='')}"
    return f"{base}?{qs}" if qs else base


def _clean_asset_name(name: str) -> str:
    text = str(name or "").strip()
    if not text or len(text) > 80 or any(ord(ch) < 32 for ch in text):
        return ""
    return text


def _host_ok(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in (DEDICATED_HOST, RELAY_HOST)


def parse_ipfs_location(value: Any) -> tuple[str, str]:
    """CID plus optional path. ipfs://, ipfs://ipfs/, CIDv0/v1, and gateway URLs."""
    if isinstance(value, dict):
        value = value.get("href") or value.get("url") or value.get("/") or ""
    if not isinstance(value, str):
        return "", ""
    raw = value.strip()
    if not raw:
        return "", ""
    if _HTTP_RE.match(raw):
        if not safe_http_url(raw):
            return "", ""
        try:
            parsed = urllib.parse.urlsplit(raw)
        except ValueError:
            return "", ""
        idx = parsed.path.lower().find("/ipfs/")
        if idx < 0:
            return "", ""
        try:
            rest = urllib.parse.unquote(parsed.path[idx + len("/ipfs/") :])
        except ValueError:
            return "", ""
        return _cid_and_path(rest)
    if re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.IGNORECASE) and not raw.lower().startswith("ipfs:"):
        return "", ""
    if raw.lower().startswith("ipfs://"):
        raw = raw[7:]
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    return _cid_and_path(raw)


def _cid_and_path(rest: str) -> tuple[str, str]:
    parts = [part for part in rest.split("/") if part not in ("", ".")]
    while parts and parts[0].lower() == "ipfs":
        parts.pop(0)
    if not parts:
        return "", ""
    if valid_cid(parts[0]):
        return parts[0], safe_rel_path("/".join(parts[1:]))
    return "", safe_rel_path("/".join(parts))


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


def _media_ref(value: Any, root_cid: str = "") -> dict[str, str] | None:
    if isinstance(value, dict):
        value = value.get("href") or value.get("url") or value.get("/") or ""
    if not isinstance(value, str) or not value.strip():
        return None
    cid, path = parse_ipfs_location(value)
    if cid:
        return {"kind": "ipfs", "src": gateway_file_url(cid, path), "cid": cid, "path": path}
    if path and valid_cid(root_cid):
        return {"kind": "ipfs", "src": gateway_file_url(root_cid, path), "cid": root_cid, "path": path}
    if value.strip().lower().startswith(("http://", "https://")):
        safe = safe_http_url(value)
        if safe:
            # Not embedded. Callers may show it as a link. We do not fetch this host.
            return {"kind": "url", "src": safe, "cid": "", "path": ""}
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
    extras = nft.get("extras") if isinstance(nft.get("extras"), list) else []
    name = nft.get("name") or ""
    description = nft.get("description") or ""
    external = nft.get("external_url") or ""
    website = nft.get("website_url") or ""
    ticker = nft.get("ticker") or ""
    issuer = nft.get("issuer") or ""
    if not any((name, description, external, website, ticker, issuer, image, attrs, extras)):
        return None
    view: dict[str, Any] = {
        "name": name,
        "description": description,
        "external_url": external,
        "website_url": website,
        "ticker": ticker,
        "issuer": issuer,
        "attributes": attrs,
        "extras": extras,
    }
    if image:
        view["image"] = image
    if isinstance(nft.get("animation"), dict):
        view["animation"] = nft["animation"]
    if isinstance(nft.get("audio"), dict):
        view["audio"] = nft["audio"]
    return view


def _ext_kind(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[-1]
    if ext == "svg":
        return "svg"
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "avif", "bmp"):
        return "image"
    if ext in ("mp4", "webm", "mov", "ogg"):
        return "video"
    if ext in ("mp3", "wav", "oga", "flac", "m4a", "aac"):
        return "audio"
    return ""


def extract_nft_media(meta: dict, root_cid: str = "") -> dict[str, Any]:
    image = None
    # xcoin-asset-1: image_url first (same as Launch), then icon / ERC-721 image.
    for key in ("image_url", "icon", "image", "img", "imageUrl", "thumbnail", "preview"):
        image = _media_ref(meta.get(key), root_cid)
        if image:
            break
    animation = None
    for key in ("animation_url", "animationUrl", "video", "video_url", "animation"):
        animation = _media_ref(meta.get(key), root_cid)
        if animation:
            break
    audio = None
    for key in ("audio", "audio_url", "audioUrl"):
        audio = _media_ref(meta.get(key), root_cid)
        if audio:
            break
    website = safe_http_url(meta.get("website_url") or "")
    external = safe_http_url(meta.get("external_url") or "") or safe_http_url(meta.get("externalUrl") or "")
    extras: list[dict[str, str]] = []
    kind_label = plain_text(meta.get("type") or "", 40)
    if kind_label:
        extras.append({"label": "Type", "value": kind_label})
    return {
        "name": plain_text(meta.get("name") or meta.get("title") or "", 80),
        "description": plain_text(meta.get("description") or meta.get("text") or "", 2000, keep_breaks=True),
        "external_url": external or website,
        "website_url": website if website and website != (external or website) else "",
        "ticker": plain_text(meta.get("symbol") or meta.get("ticker") or "", 20),
        "issuer": plain_text(meta.get("issuer") or "", 80),
        "image": image,
        "animation": animation,
        "audio": audio,
        "attributes": read_traits(meta),
        "extras": extras,
    }


_SVG_BLOCK = re.compile(
    r"<\s*(script|foreignObject|iframe|embed|object)\b[^>]*>.*?<\s*/\s*\1\s*>"
    r"|<\s*(script|foreignObject|iframe|embed|object)\b[^>]*/\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SVG_ON = re.compile(r"""\s+on[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_SVG_JS = re.compile(
    r"""(href|xlink:href|src)\s*=\s*(["'])\s*javascript:[^"']*\2""",
    re.IGNORECASE,
)
_JSON_NAMES = ("metadata.json", "asset.json", "nft.json", "index.json")


def sanitize_svg(raw: bytes) -> bytes:
    """Drop active content. The page still shows this only as an img."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return b""
    if "<svg" not in text.lower():
        return b""
    text = re.sub(r"<!DOCTYPE[^>\[]*\[[^\]]*\]>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<!ENTITY[^>]*>", "", text, flags=re.IGNORECASE)
    text = _SVG_BLOCK.sub("", text)
    text = _SVG_ON.sub("", text)
    text = _SVG_JS.sub("", text)
    if "<svg" not in text.lower():
        return b""
    return text.encode("utf-8")


def _read_body(resp, url: str, limit: int, peek: bool) -> tuple[bytes, str, str, bool]:
    ct = resp.headers.get("content-type") or ""
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes():
        total += len(chunk)
        chunks.append(chunk)
        raw = b"".join(chunks)
        if _is_gateway_miss(ct, raw):
            raise ValueError("unpinned/miss")
        if peek:
            kind, _sniffed = _sniff_kind(ct, raw[:MAX_PEEK_BYTES])
            if kind in ("image", "video", "audio") and total >= min(4096, MAX_PEEK_BYTES):
                return raw[:MAX_PEEK_BYTES], ct, url, True
            if kind in ("json", "text") and total > JSON_MAX_BYTES:
                return raw[:JSON_MAX_BYTES], ct, url, True
            if kind not in ("json", "text") and total > MAX_PEEK_BYTES:
                return raw[:MAX_PEEK_BYTES], ct, url, True
        elif total > limit:
            raise ValueError("IPFS object exceeds explorer size limit")
    return b"".join(chunks), ct, url, False


def _http_get(url: str, limit: int, peek: bool, timeout: float) -> tuple[bytes, str, str, bool]:
    if not _host_ok(url):
        raise ValueError("refused host")
    headers = {"User-Agent": "XFER-Explorer/1.0", "Accept": "*/*"}
    current = url
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
            for _hop in range(3):
                if not _host_ok(current):
                    raise ValueError("refused host")
                with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location") or ""
                        current = urllib.parse.urljoin(current, loc)
                        continue
                    if resp.status_code >= 400:
                        raise ValueError(f"{resp.status_code} from {current}")
                    return _read_body(resp, current, limit, peek)
    except httpx.HTTPError as exc:
        raise ValueError(f"IPFS fetch failed: {exc}") from exc
    raise ValueError("IPFS fetch failed: too many redirects")


def _fetch(cid: str, limit: int, peek: bool = False, path: str = "") -> tuple[bytes, str, str, bool]:
    if not valid_cid(cid):
        raise ValueError("invalid IPFS CID")
    rel = safe_rel_path(path)
    url = gateway_file_url(cid, rel)
    timeout = JSON_FETCH_TIMEOUT if peek else FETCH_TIMEOUT
    try:
        return _http_get(url, limit, peek, timeout)
    except ValueError as exc:
        text = str(exc)
        if text.startswith("IPFS fetch failed"):
            raise
        raise ValueError(f"IPFS fetch failed: {text}") from exc


def _apply_json(out: dict[str, Any], meta: Any, root_cid: str = "") -> None:
    cleaned = _clean_json(meta)
    if cleaned is not None:
        out["metadata"] = cleaned
    if isinstance(meta, dict):
        nft = extract_nft_media(meta, root_cid)
        out["nft"] = nft
        view = metadata_view(nft)
        if view:
            out["view"] = view
    raw_json = public_json_text(meta)
    if raw_json:
        out["raw_json"] = raw_json


def _blank(cid: str, kind: str, content_type: str, source: str) -> dict[str, Any]:
    return {
        "cid": cid,
        "kind": kind,
        "content_type": content_type,
        "size": 0,
        "truncated": False,
        "source": source,
        "url": ipfs_content_url(cid),
        "gateways": gateway_urls(cid),
    }


def _directory_names(cid: str) -> list[str] | None:
    """Named links mean a folder. File chunks have empty names."""
    url = f"{DEDICATED_GATEWAY}{cid}?format=dag-json"
    try:
        raw, _ct, _source, truncated = _http_get(url, JSON_MAX_BYTES, peek=False, timeout=JSON_FETCH_TIMEOUT)
    except ValueError:
        return None
    if truncated:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    links = doc.get("Links") if isinstance(doc, dict) else None
    if not isinstance(links, list):
        return None
    names: list[str] = []
    for link in links[:64]:
        if not isinstance(link, dict):
            continue
        name = link.get("Name") if isinstance(link.get("Name"), str) else ""
        safe = safe_rel_path(name)
        if safe and "/" not in safe:
            names.append(safe)
    return names or None


def _pick_json_name(names: list[str]) -> str:
    folded = {name.lower(): name for name in names}
    for preferred in _JSON_NAMES:
        if preferred in folded:
            return folded[preferred]
    json_names = [name for name in names if name.lower().endswith(".json")]
    if len(json_names) == 1:
        return json_names[0]
    return json_names[0] if json_names else ""


def _pick_media_name(names: list[str]) -> str:
    for name in names:
        if _ext_kind(name) in ("image", "svg", "video", "audio"):
            return name
    return ""


def _inspect_directory(cid: str, names: list[str]) -> dict[str, Any]:
    out = _blank(cid, "unknown", "application/vnd.ipfs.directory", gateway_file_url(cid))
    out["directory"] = names[:64]
    json_name = _pick_json_name(names)
    if json_name:
        try:
            raw, content_type, source, truncated = _fetch(cid, JSON_MAX_BYTES, peek=True, path=json_name)
        except ValueError:
            raw, content_type, source, truncated = b"", "", "", False
        out["source"] = source or out["source"]
        out["content_type"] = content_type or out["content_type"]
        out["size"] = len(raw)
        out["truncated"] = truncated
        out["path"] = json_name
        if raw and not truncated:
            try:
                meta = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                meta = None
            if meta is not None:
                out["kind"] = "json"
                _apply_json(out, meta, cid)
                return out
    media_name = _pick_media_name(names)
    if media_name:
        kind = _ext_kind(media_name) or "other"
        out["kind"] = kind
        out["path"] = media_name
        ref = {
            "kind": "ipfs",
            "cid": cid,
            "path": media_name,
            "src": gateway_file_url(cid, media_name),
        }
        if kind in ("image", "svg"):
            out["nft"] = {"name": "", "description": "", "external_url": "", "image": ref, "attributes": []}
        elif kind == "video":
            out["nft"] = {"name": "", "description": "", "external_url": "", "animation": ref, "attributes": []}
        elif kind == "audio":
            out["nft"] = {"name": "", "description": "", "external_url": "", "audio": ref, "attributes": []}
    return out


def _fill_from_bytes(cid: str, raw: bytes, content_type: str, source: str, truncated: bool) -> dict[str, Any]:
    kind, sniffed = _sniff_kind(content_type, raw)
    if sniffed == "image/svg+xml":
        kind = "svg"
    out = _blank(cid, kind, sniffed, source)
    out["size"] = len(raw)
    out["truncated"] = truncated
    if kind == "json" and not truncated:
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return out
        _apply_json(out, meta, "")
    elif kind == "text" and not truncated:
        out["text"] = raw.decode("utf-8", errors="replace")[:8000]
    return out


def _inspect_uncached(cid: str) -> dict[str, Any]:
    raw, content_type, source, truncated = _fetch(cid, JSON_MAX_BYTES, peek=True)
    kind, _sniffed = _sniff_kind(content_type, raw)
    # A folder CID is a listing (or an unknown body), not the picture itself.
    if kind in ("text", "other") and not _is_gateway_miss(content_type, raw):
        names = _directory_names(cid)
        if names:
            return _inspect_directory(cid, names)
    return _fill_from_bytes(cid, raw, content_type, source, truncated)


def _presentation(info: dict[str, Any], name: str = "") -> dict[str, Any]:
    """URLs the page may embed. Our gateway and the Launch media route only."""
    nft = info.get("nft") if isinstance(info.get("nft"), dict) else {}
    image = nft.get("image") if isinstance(nft.get("image"), dict) else {}
    animation = nft.get("animation") if isinstance(nft.get("animation"), dict) else {}
    audio = nft.get("audio") if isinstance(nft.get("audio"), dict) else {}
    kind = info.get("kind") or ""
    path = info.get("path") if isinstance(info.get("path"), str) else ""

    def urls(cid: str, rel: str) -> list[str]:
        if not valid_cid(cid):
            return []
        return [gateway_file_url(cid, rel), relay_media_url(cid, rel, name)]

    def ref_kind(ref: dict) -> str:
        rel = ref.get("path") or ""
        return _ext_kind(rel) if isinstance(rel, str) else ""

    anim_kind = ref_kind(animation) or ("video" if animation.get("cid") or animation.get("kind") == "ipfs" else "")
    if animation.get("kind") == "url":
        anim_kind = ""
    if kind == "video" or anim_kind == "video" or ref_kind(image) == "video":
        if anim_kind == "video":
            target, rel = animation.get("cid") or "", animation.get("path") or ""
        elif ref_kind(image) == "video":
            target, rel = image.get("cid") or "", image.get("path") or ""
        else:
            target, rel = info.get("cid") or "", path
        poster = urls(image.get("cid") or "", image.get("path") or "") if image.get("cid") and image.get("cid") != target else []
        return {"type": "video", "urls": urls(target, rel), "poster": poster}
    if kind == "audio" or audio.get("cid"):
        if audio.get("cid"):
            return {"type": "audio", "urls": urls(audio.get("cid") or "", audio.get("path") or ""), "poster": []}
        return {"type": "audio", "urls": [], "poster": []}
    if kind in ("image", "svg"):
        shown = "svg" if kind == "svg" or ref_kind(image) == "svg" else "image"
        rel = path or (image.get("path") or "")
        target = image.get("cid") if image.get("cid") and rel else info.get("cid")
        if not rel:
            target = info.get("cid")
        return {"type": shown, "urls": urls(target or "", rel if target == image.get("cid") else ""), "poster": []}
    if image.get("cid"):
        shown = ref_kind(image) or "image"
        if shown == "audio":
            return {"type": "audio", "urls": urls(image["cid"], image.get("path") or ""), "poster": []}
        if shown not in ("image", "svg", "video"):
            shown = "image"
        return {"type": shown, "urls": urls(image["cid"], image.get("path") or ""), "poster": []}
    return {"type": "none", "urls": [], "poster": []}


def _with_presentation(info: dict[str, Any], name: str = "") -> dict[str, Any]:
    info = json.loads(json.dumps(info))
    info["presentation"] = _presentation(info, name)
    return info


def inspect_cid(cid: str) -> dict[str, Any]:
    cid = normalize_ipfs(cid)
    if not valid_cid(cid):
        raise ValueError("invalid IPFS CID")
    cached = _cache_get(cid)
    if cached is not None:
        return _with_presentation(cached, "")
    out = _inspect_uncached(cid)
    _cache_put(cid, out)
    return _with_presentation(_cache_get(cid) or out, "")


def _head_content_type(cid: str) -> str:
    url = gateway_file_url(cid)
    if not _host_ok(url):
        return ""
    try:
        with httpx.Client(timeout=8.0, follow_redirects=False, headers={"User-Agent": "XFER-Explorer/1.0"}) as client:
            resp = client.head(url)
        if resp.status_code >= 400:
            return ""
        return (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    except httpx.HTTPError:
        return ""


def _overlay_relay_media(nft: dict[str, Any], body: dict, cid: str) -> None:
    """Use paths the relay already resolved inside a folder."""
    image = nft.get("image") if isinstance(nft.get("image"), dict) else None
    if not (image and image.get("cid")):
        rel = safe_rel_path(body.get("imagePath") or "")
        root = normalize_ipfs(body.get("imageRoot") or "") or cid
        image_cid = normalize_ipfs(body.get("imageCid") or "")
        if rel and valid_cid(root):
            nft["image"] = {"kind": "ipfs", "cid": root, "path": rel, "src": gateway_file_url(root, rel)}
        elif image_cid:
            nft["image"] = {"kind": "ipfs", "cid": image_cid, "path": "", "src": gateway_file_url(image_cid)}
    animation = nft.get("animation") if isinstance(nft.get("animation"), dict) else None
    if not (animation and (animation.get("cid") or animation.get("src"))):
        rel = safe_rel_path(body.get("animationPath") or "")
        root = normalize_ipfs(body.get("animationRoot") or "") or cid
        anim_cid = normalize_ipfs(body.get("animationCid") or "")
        if rel and valid_cid(root):
            nft["animation"] = {"kind": "ipfs", "cid": root, "path": rel, "src": gateway_file_url(root, rel)}
        elif anim_cid:
            nft["animation"] = {"kind": "ipfs", "cid": anim_cid, "path": "", "src": gateway_file_url(anim_cid)}


def _from_relay(body: dict, cid: str) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    relay_cid = normalize_ipfs(str(body.get("cid") or "")) or cid
    if relay_cid != cid:
        return None
    kind = str(body.get("kind") or "unknown").lower()
    if kind not in ("json", "image", "video", "audio", "svg", "unknown"):
        kind = "unknown"
    raw_text = body.get("rawJson") if isinstance(body.get("rawJson"), str) else ""
    meta = None
    if raw_text:
        try:
            meta = json.loads(raw_text)
        except json.JSONDecodeError:
            meta = None
    root = ""
    if body.get("imageRoot") or body.get("imagePath"):
        root = normalize_ipfs(str(body.get("imageRoot") or "")) or cid
    if isinstance(meta, dict):
        nft = extract_nft_media(meta, root if kind == "json" else "")
    else:
        attrs = body.get("attributes") if isinstance(body.get("attributes"), list) else []
        nft = {
            "name": plain_text(body.get("name") or "", 80),
            "description": plain_text(body.get("description") or "", 2000, keep_breaks=True),
            "external_url": safe_http_url(body.get("externalUrl") or "") or safe_http_url(body.get("websiteUrl") or ""),
            "website_url": "",
            "ticker": plain_text(body.get("ticker") or "", 20),
            "issuer": plain_text(body.get("issuer") or "", 80),
            "image": None,
            "animation": None,
            "audio": None,
            "attributes": read_traits({"attributes": attrs}),
            "extras": [],
        }
        website = safe_http_url(body.get("websiteUrl") or "")
        external = safe_http_url(body.get("externalUrl") or "")
        nft["external_url"] = external or website
        nft["website_url"] = website if website and website != nft["external_url"] else ""
        for row in (body.get("extras") or [])[:12]:
            if not isinstance(row, dict):
                continue
            label = plain_text(row.get("label") or "", 40)
            value = plain_text(row.get("value") or "", 80)
            if label and value:
                nft["extras"].append({"label": label, "value": value})
    _overlay_relay_media(nft, body, cid)
    # A raw picture/video is the CID itself. Relay currently labels some videos as image.
    file_is_media = kind in ("image", "unknown") and not safe_rel_path(str(body.get("imagePath") or ""))
    image = nft.get("image") if isinstance(nft.get("image"), dict) else {}
    if file_is_media and (not image.get("cid") or image.get("cid") == cid):
        sniffed = _head_content_type(cid)
        if sniffed.startswith("video/"):
            kind = "video"
        elif sniffed.startswith("audio/"):
            kind = "audio"
        elif sniffed == "image/svg+xml":
            kind = "svg"
        elif sniffed.startswith("image/"):
            kind = "image"
    out = _blank(cid, kind, kind, f"{RELAY_LAUNCH_IPFS}{cid}")
    out["nft"] = nft
    view = metadata_view(nft)
    if view:
        out["view"] = view
    if isinstance(meta, (dict, list)):
        text = public_json_text(meta)
        if text:
            out["raw_json"] = text
        cleaned = _clean_json(meta)
        if cleaned is not None:
            out["metadata"] = cleaned
    elif raw_text and "<" not in raw_text:
        out["raw_json"] = raw_text[:RAW_JSON_CAP]
    return out


def _relay_document(cid: str, name: str) -> dict | None:
    clean = _clean_asset_name(name)
    query = urllib.parse.urlencode({"name": clean}) if clean else ""
    suffix = f"?{query}" if query else ""
    enc = urllib.parse.quote(cid, safe="")
    urls = (
        f"{RELAY_LAUNCH_IPFS}{enc}{suffix}",
        f"{RELAY_ASSETS_IPFS}{enc}{suffix}",
    )
    for url in urls:
        try:
            raw, content_type, _source, truncated = _http_get(url, JSON_MAX_BYTES, peek=False, timeout=JSON_FETCH_TIMEOUT)
        except ValueError:
            continue
        looks_json = "json" in content_type.lower() or raw.lstrip()[:1] == b"{"
        if truncated or not looks_json:
            continue
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict) and not doc.get("error"):
            return doc
    return None


def inspect_asset_ipfs(cid: str, name: str = "") -> dict[str, Any]:
    """Relay first (Launch's guarded routes). Self-fetch only from our gateway."""
    cid = normalize_ipfs(cid)
    if not valid_cid(cid):
        raise ValueError("invalid IPFS CID")
    clean = _clean_asset_name(name)
    cache_id = cid + "\n" + clean
    cached = _cache_get(cache_id)
    if cached is not None:
        return _with_presentation(cached, clean)
    relay = _relay_document(cid, clean) if clean else None
    parsed = _from_relay(relay, cid) if relay else None
    if parsed is None:
        parsed = _inspect_uncached(cid)
    _cache_put(cache_id, parsed)
    return _with_presentation(_cache_get(cache_id) or parsed, clean)


def fetch_content(cid: str) -> tuple[bytes, str]:
    cid = normalize_ipfs(cid)
    raw, content_type, _source, _truncated = _fetch(cid, MAX_CONTENT_BYTES, peek=False)
    _kind, sniffed = _sniff_kind(content_type, raw)
    if sniffed == "image/svg+xml":
        raw = sanitize_svg(raw)
        if not raw:
            raise ValueError("SVG rejected")
    return raw, sniffed
