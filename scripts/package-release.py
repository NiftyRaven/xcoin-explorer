"""Build a public release zip with no secrets, venv, or chain data."""

from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "data",
}
SKIP_FILE_NAMES = {
    "explorer.toml",
    ".env",
    ".cookie",
    "start-wallet-for-explorer.bat",
    "xattestor.key",
    "host-guests.json",
}
SKIP_SUFFIXES = {".db", ".db-wal", ".db-shm", ".pem", ".key", ".log", ".zip"}


def version() -> str:
    text = (ROOT / "explorer" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("missing __version__")


def include(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if set(rel.parts) & SKIP_DIR_NAMES:
        return False
    if path.name in SKIP_FILE_NAMES:
        return False
    if path.suffix in SKIP_SUFFIXES:
        return False
    if path.name.startswith(".env"):
        return False
    return True


def main() -> None:
    ver = version()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    name = f"XFER-Explorer-{ver}"
    zip_path = dist / f"{name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    files = [p for p in ROOT.rglob("*") if p.is_file() and include(p)]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            zf.write(path, arcname=str(Path(name) / path.relative_to(ROOT)))
    print(zip_path)
    print(f"{len(files)} files")


if __name__ == "__main__":
    main()
