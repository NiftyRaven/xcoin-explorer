"""Pick an official X Coin wallet zip from GitHub Releases.

Works with Light and Heavy, including tags like v1.0.16-light.
Does not trust GitHub's "latest" flag (that can stay on an older Light
release while a newer Heavy exists).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Last-known-good if the Releases API is unreachable (private repo, no net).
FALLBACK_VERSION = "1.0.15"
FALLBACK_LINUX = (
    f"https://github.com/NiftyRaven/x-coin/releases/download/v{FALLBACK_VERSION}/"
    f"X-Coin-{FALLBACK_VERSION}-Linux-x86_64.tar.gz"
)
FALLBACK_WINDOWS = (
    f"https://github.com/NiftyRaven/x-coin/releases/download/v{FALLBACK_VERSION}/"
    f"X-Coin-{FALLBACK_VERSION}-Windows.zip"
)

_LINUX_NAME = re.compile(r"^X-Coin-.+-Linux-x86_64\.tar\.gz$")
_WINDOWS_NAME = re.compile(r"^X-Coin-.+-Windows\.zip$")


def parse_version(tag: str) -> tuple[int, ...] | None:
    """v1.0.16, 1.0.16-light, v1.0.16-heavy → (1, 0, 16)."""
    s = (tag or "").strip()
    if s[:1] in "vV":
        s = s[1:]
    parts: list[int] = []
    cur = ""
    for c in s + ".":
        if c.isdigit():
            cur += c
        elif c == ".":
            if not cur:
                break
            parts.append(int(cur))
            cur = ""
        else:
            if cur:
                parts.append(int(cur))
            break
    if len(parts) < 2:
        return None
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def _edition_rank(rel: dict[str, Any], asset_name: str) -> int:
    blob = " ".join(
        [
            str(rel.get("name") or ""),
            str(rel.get("tag_name") or ""),
            asset_name,
        ]
    ).lower()
    # Prefer Heavy when two zips share the same numeric version — explorer
    # still works on Light; Heavy just has more optional RPCs.
    if "heavy" in blob:
        return 2
    if "light" in blob:
        return 1
    return 0


def pick_wallet_asset(
    releases: Iterable[Any],
    *,
    name_re: re.Pattern[str],
) -> tuple[str, str] | None:
    """Return (version_label, download_url) for the newest official zip."""
    best: tuple[tuple, int, str, str] | None = None
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        if rel.get("draft") or rel.get("prerelease"):
            continue
        key = parse_version(str(rel.get("tag_name") or ""))
        if not key:
            continue
        for asset in rel.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            if not url or not name_re.match(name):
                continue
            rank = _edition_rank(rel, name)
            label = ".".join(str(p) for p in key[:3])
            cand = (key, rank, label, url)
            if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1] > best[1]):
                best = cand
            break
    if not best:
        return None
    return best[2], best[3]


def pick_linux_tarball(releases: Iterable[Any]) -> tuple[str, str] | None:
    return pick_wallet_asset(releases, name_re=_LINUX_NAME)


def pick_windows_zip(releases: Iterable[Any]) -> tuple[str, str] | None:
    return pick_wallet_asset(releases, name_re=_WINDOWS_NAME)
