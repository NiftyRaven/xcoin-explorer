"""Coin supply figures for listing sites (CoinGecko / CoinMarketCap style).

All math is in atoms (1 XFER = 100,000,000 xferons). Nothing here is a float.

- issued      = block subsidy paid from height 1 through the indexed tip
                (the same number as ``supply_atoms`` in /api/status).
- burned      = XFER sitting at the chain's burn addresses. These addresses
                come from chainparams.cpp. Nobody holds a key for them, so
                coins sent there can never move again.
- total       = issued - burned.
- circulating = total. The chain has no locked, vesting, team, or premine
                coins, and the explorer treats nothing else as unspendable.
- max         = every coin the subsidy schedule will ever pay
                (``lifetime_atoms`` in /api/stats).

XFER sent to an OP_RETURN output is destroyed too, but the indexer stores
those outputs with zero value, so it cannot subtract them. Ordinary OP_RETURN
memos carry no XFER.
"""

from __future__ import annotations

import threading
import time

from explorer.chain import (
    COIN,
    circulating_supply,
    halving_interval_for_network,
    lifetime_supply,
)

CACHE_SECONDS = 60

# Burn addresses from NiftyRaven/x-coin src/chainparams.cpp.
_MAIN_BURNS = {
    "XissueAssetXXXXXXXXXXXXXXXXXXwTyxt": "issue root asset",
    "XreissueAssetXXXXXXXXXXXXXXXZNfDqa": "reissue asset",
    "XissueSubAssetXXXXXXXXXXXXXXcHkFpF": "issue sub asset",
    "XissueUniqueAssetXXXXXXXXXXXagKZDZ": "issue unique asset",
    "XissueMsgChanneLAssetXXXXXXXcZDf2U": "issue message channel",
    "XissueQuaLifierXXXXXXXXXXXXXXAQP3h": "issue qualifier",
    "XissueSubQuaLifierXXXXXXXXXXb8QkLq": "issue sub qualifier",
    "XissueRestrictedXXXXXXXXXXXXU7kfQh": "issue restricted asset",
    "XnuLLTagBurnXXXXXXXXXXXXXXXXdbJo3F": "add null qualifier tag",
    "XgLobaLBurnXXXXXXXXXXXXXXXXXZTDEwo": "global burn",
}
# Testnet and regtest share one set.
_TEST_BURNS = {
    "yissueAssetXXXXXXXXXXXXXXXXXa53BzP": "issue root asset",
    "yReissueAssetXXXXXXXXXXXXXXXcgSAHx": "reissue asset",
    "yissueSubAssetXXXXXXXXXXXXXXXLKrKM": "issue sub asset",
    "yissueUniqueAssetXXXXXXXXXXXXYCYqw": "issue unique asset",
    "yissueMsgChanneLAssetXXXXXXXcMfYPL": "issue message channel",
    "yissueQuaLifierXXXXXXXXXXXXXXszhtz": "issue qualifier",
    "yissueSubQuaLifierXXXXXXXXXXaJd6nj": "issue sub qualifier",
    "yissueRestrictedXXXXXXXXXXXXWy4AP5": "issue restricted asset",
    "yaddTagBurnXXXXXXXXXXXXXXXXXVtnNZw": "add null qualifier tag",
    "ygLobaLBurnXXXXXXXXXXXXXXXXXcrYmcW": "global burn",
}
BURN_ADDRESSES = {"main": _MAIN_BURNS, "test": _TEST_BURNS, "regtest": _TEST_BURNS}


def burn_addresses(network: str) -> dict[str, str]:
    return BURN_ADDRESSES.get(network or "main", _MAIN_BURNS)


def format_xfer(atoms: int) -> str:
    """Exact decimal XFER: 17746500000000000 -> '177465000', 12345 -> '0.00012345'."""
    atoms = int(atoms)
    sign = "-" if atoms < 0 else ""
    whole, frac = divmod(abs(atoms), COIN)
    if not frac:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{frac:08d}".rstrip("0")


def burned_by_address(db, network: str) -> dict[str, int]:
    """XFER (atoms) at each burn address. One indexed query on ``utxos``."""
    addrs = list(burn_addresses(network))
    marks = ",".join("?" * len(addrs))
    rows = db.conn.execute(
        f"SELECT address, COALESCE(SUM(value),0) AS v FROM utxos WHERE address IN ({marks}) GROUP BY address",
        tuple(addrs),
    ).fetchall()
    out: dict[str, int] = {}
    for row in rows or []:
        try:
            addr, value = row["address"], int(row["v"] or 0)
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if addr and value > 0:
            out[addr] = value
    return out


def compute_supply(db, network: str | None = None) -> dict:
    net = network or db.get_meta("network") or "main"
    interval = halving_interval_for_network(net)
    height = max(int(db.indexed_height()), 0)
    issued = circulating_supply(height, interval)
    by_addr = burned_by_address(db, net)
    burned = min(sum(by_addr.values()), issued)
    total = issued - burned
    locked = 0
    circulating = total - locked
    max_atoms = lifetime_supply(interval)
    labels = burn_addresses(net)
    return {
        "ticker": "XFER",
        "network": net,
        "height": height,
        "circulating": format_xfer(circulating),
        "total": format_xfer(total),
        "max": format_xfer(max_atoms),
        "circulating_atoms": circulating,
        "total_atoms": total,
        "max_atoms": max_atoms,
        "issued_atoms": issued,
        "burned_atoms": burned,
        "locked_atoms": locked,
        "atoms_per_xfer": COIN,
        "burns": [
            {"address": a, "label": labels.get(a, "burn"), "atoms": v, "xfer": format_xfer(v)}
            for a, v in sorted(by_addr.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "definitions": {
            "total": "subsidy issued through this height, minus XFER held at burn addresses",
            "circulating": "same as total; the chain has no locked or vesting coins",
            "max": "every coin the subsidy schedule will ever pay",
        },
        "cache_seconds": CACHE_SECONDS,
    }


class SupplyCache:
    """Keeps one supply snapshot for ``ttl`` seconds so listing-site polls stay cheap."""

    def __init__(self, db, ttl: float = CACHE_SECONDS, clock=time.monotonic):
        self._db = db
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._at = 0.0

    def get(self) -> dict:
        with self._lock:
            now = self._clock()
            if self._value is None or now - self._at >= self._ttl:
                try:
                    self._value = compute_supply(self._db)
                except Exception:
                    # A failed read keeps serving the last good numbers.
                    if self._value is None:
                        raise
                    return self._value
                self._at = now
            return self._value
