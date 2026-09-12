"""XFER / xferon formatting."""

from __future__ import annotations

from explorer.chain import COIN, TICKER


def atoms_to_xfer(atoms: int | float | None) -> float:
    if atoms is None:
        return 0.0
    return int(atoms) / COIN


def xfer_to_atoms(value: float | str | int) -> int:
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return 0
        value = float(value)
    return int(round(float(value) * COIN))


def format_xfer(atoms: int | float | None, *, ticker: bool = True, decimals: int | None = None) -> str:
    if atoms is None:
        atoms = 0
    atoms = int(atoms)
    sign = "-" if atoms < 0 else ""
    atoms = abs(atoms)
    whole = atoms // COIN
    frac = atoms % COIN
    if decimals is None:
        if frac == 0:
            body = f"{whole:,}"
        else:
            frac_s = f"{frac:08d}".rstrip("0")
            body = f"{whole:,}.{frac_s}"
    else:
        body = f"{atoms / COIN:,.{decimals}f}"
    if ticker:
        return f"{sign}{body} {TICKER}"
    return f"{sign}{body}"


def format_compact(atoms: int | float | None) -> str:
    if atoms is None:
        atoms = 0
    n = abs(int(atoms)) / COIN
    sign = "-" if int(atoms) < 0 else ""
    if n >= 1_000_000_000:
        return f"{sign}{n / 1_000_000_000:.2f}B {TICKER}"
    if n >= 1_000_000:
        return f"{sign}{n / 1_000_000:.2f}M {TICKER}"
    if n >= 1_000:
        return f"{sign}{n / 1_000:.2f}k {TICKER}"
    return format_xfer(atoms)
