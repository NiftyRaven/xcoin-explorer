"""Resolve Ravencoin-style asset IPFS hashes for the explorer UI."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from explorer.decode import normalize_ipfs

CID_RE = re.compile(
    r"^(Qm[1-9A-HJ-NP-Za-km-z]{44}|baf[a-z2-7]{50,})$"
)

GATEWAYS = (
    "https://ipfs.io/ipfs/",
    "https://dweb.link/ipfs/",
    "https://w3s.link/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
)

MAX_INSPECT_BYTES = 2 * 1024 * 1024
MAX_PEEK_BYTES = 64 * 1024
MAX_CONTENT_BYTES = 80 * 1024 * 1024
FETCH_TIMEOUT = 22.0

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
        return {"kind": "ipfs", "src": f"/api/ipfs/content/{cid}", "cid": cid}
    return None


def extract_nft_media(meta: dict) -> dict[str, Any]:
    image = None
    for key in ("image", "image_url", "imageUrl", "img", "thumbnail", "preview"):
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
    attrs = meta.get("attributes") or meta.get("traits") or []
    if not isinstance(attrs, list):
        attrs = []
    return {
        "name": meta.get("name") or meta.get("title") or "",
        "description": meta.get("description") or meta.get("text") or "",
        "external_url": meta.get("external_url") or meta.get("externalUrl") or "",
        "image": image,
        "animation": animation,
        "audio": audio,
        "attributes": attrs,
    }


def _fetch(cid: str, limit: int, peek: bool = False) -> tuple[bytes, str, str, bool]:
    if not valid_cid(cid):
        raise ValueError("invalid IPFS CID")
    last_err = "unreachable"
    headers = {"User-Agent": "XFER-Explorer/1.0"}
    for base in GATEWAYS:
        url = base + cid
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
                with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        last_err = f"{resp.status_code} from {base}"
                        continue
                    ct = resp.headers.get("content-type") or ""
                    chunks: list[bytes] = []
                    total = 0
                    truncated = False
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        chunks.append(chunk)
                        raw = b"".join(chunks)
                        if peek:
                            kind, _sniffed = _sniff_kind(ct, raw[:MAX_PEEK_BYTES])
                            if kind in ("image", "video", "audio") and total >= min(4096, MAX_PEEK_BYTES):
                                truncated = True
                                return raw[:MAX_PEEK_BYTES], ct, url, truncated
                            if kind == "json" and total > MAX_INSPECT_BYTES:
                                raise ValueError("IPFS object exceeds explorer size limit")
                            if kind not in ("json", "text") and total > MAX_PEEK_BYTES:
                                truncated = True
                                return raw[:MAX_PEEK_BYTES], ct, url, truncated
                        elif total > limit:
                            raise ValueError("IPFS object exceeds explorer size limit")
                    return b"".join(chunks), ct, url, False
        except httpx.HTTPError as e:
            last_err = str(e)
            continue
    raise ValueError(f"IPFS fetch failed: {last_err}")


def inspect_cid(cid: str) -> dict[str, Any]:
    cid = normalize_ipfs(cid)
    if not valid_cid(cid):
        raise ValueError("invalid IPFS CID")
    raw, content_type, source, truncated = _fetch(cid, MAX_INSPECT_BYTES, peek=True)
    kind, sniffed = _sniff_kind(content_type, raw)
    out: dict[str, Any] = {
        "cid": cid,
        "kind": kind,
        "content_type": sniffed,
        "size": len(raw),
        "truncated": truncated,
        "source": source,
        "url": f"/api/ipfs/content/{cid}",
        "gateways": gateway_urls(cid),
    }
    if kind == "json" and not truncated:
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"invalid IPFS JSON: {e}") from e
        if isinstance(meta, dict):
            out["metadata"] = meta
            out["nft"] = extract_nft_media(meta)
        else:
            out["metadata"] = meta
    elif kind == "text":
        out["text"] = raw.decode("utf-8", errors="replace")[:8000]
    return out


def fetch_content(cid: str) -> tuple[bytes, str]:
    cid = normalize_ipfs(cid)
    raw, content_type, _source, _truncated = _fetch(cid, MAX_CONTENT_BYTES, peek=False)
    _kind, sniffed = _sniff_kind(content_type, raw)
    return raw, sniffed
