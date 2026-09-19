"""Load explorer settings from env, optional TOML, and a local X Coin node."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from explorer.chain import BLOCK_TIME_SECONDS

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    tomllib = None  # type: ignore


def _windows_appdata() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata)
    return Path.home() / "AppData" / "Roaming"


def default_datadirs() -> list[Path]:
    dirs: list[Path] = []
    if os.name == "nt":
        base = _windows_appdata() / "XCoin"
        dirs.extend([base, base / "regtest", base / "testnet1"])
    else:
        base = Path.home() / ".xcoin"
        dirs.extend([base, base / "regtest", base / "testnet1"])
        mac = Path.home() / "Library" / "Application Support" / "XCoin"
        dirs.extend([mac, mac / "regtest", mac / "testnet1"])
    return dirs


def cookie_candidates() -> list[Path]:
    extra = os.environ.get("XCOIN_COOKIE")
    out: list[Path] = []
    if extra:
        out.append(Path(extra))
    for d in default_datadirs():
        out.append(d / ".cookie")
    return out


def read_cookie(path: Path) -> tuple[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if ":" not in text:
        return None
    user, password = text.split(":", 1)
    return user, password


@dataclass
class Settings:
    rpc_host: str = "127.0.0.1"
    rpc_port: int = 0
    rpc_user: str = ""
    rpc_password: str = ""
    cookie_file: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    database: Path = field(default_factory=lambda: DATA_DIR / "xcoin-explorer.db")
    batch_size: int = 40
    poll_seconds: float = float(BLOCK_TIME_SECONDS)

    def rpc_url(self, port: int | None = None) -> str:
        p = port if port is not None else (self.rpc_port or 38442)
        return f"http://{self.rpc_host}:{p}/"

    def auth(self) -> tuple[str, str] | None:
        if self.rpc_user:
            return self.rpc_user, self.rpc_password
        if self.cookie_file:
            got = read_cookie(Path(os.path.expandvars(self.cookie_file)))
            if got:
                return got
        for path in cookie_candidates():
            got = read_cookie(path)
            if got:
                self.cookie_file = str(path)
                return got
        return None


def _load_toml(path: Path) -> dict:
    if not path.exists() or tomllib is None:
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings() -> Settings:
    cfg = _load_toml(ROOT / "explorer.toml")
    rpc = cfg.get("rpc") or {}
    exp = cfg.get("explorer") or {}
    s = Settings()
    s.rpc_host = os.environ.get("XCOIN_RPC_HOST", rpc.get("host") or s.rpc_host)
    s.rpc_port = int(os.environ.get("XCOIN_RPC_PORT", rpc.get("port") or 0) or 0)
    s.rpc_user = os.environ.get("XCOIN_RPC_USER", rpc.get("user") or "")
    s.rpc_password = os.environ.get("XCOIN_RPC_PASSWORD", rpc.get("password") or "")
    s.cookie_file = os.environ.get("XCOIN_COOKIE", rpc.get("cookie_file") or "")
    s.bind_host = os.environ.get("XCOIN_EXPLORER_HOST", exp.get("host") or s.bind_host)
    s.bind_port = int(os.environ.get("XCOIN_EXPLORER_PORT", exp.get("port") or s.bind_port))
    db = os.environ.get("XCOIN_EXPLORER_DB", exp.get("database") or str(s.database))
    s.database = Path(db)
    if not s.database.is_absolute():
        s.database = ROOT / s.database
    s.batch_size = int(exp.get("batch_size") or s.batch_size)
    s.poll_seconds = float(exp.get("poll_seconds") or s.poll_seconds)
    return s
