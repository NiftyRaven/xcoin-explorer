"""X Coin chain constants and consensus helpers.

Source of truth: NiftyRaven/x-coin whitepaper, docs/LOTTERY.md, chainparams.cpp.
"""

from __future__ import annotations

COIN = 100_000_000
TICKER = "XFER"
SUBUNIT = "xferon"
NAME = "X Coin"

GENESIS_TIME_MAIN = 1_789_197_360  # 2026-09-12 03:16:00 America/New_York (EDT)
GENESIS_HASH_MAIN = "7c790cdb7a233c020cb71346082709a185eb6577643a327349112d425a712beb"
GENESIS_MERKLE = "bacf268e26e66e3c7c3ac634652bc7765c5f6d97c796fe1b288d16b9f0e4b0b5"
INITIAL_SUBSIDY = 5000 * COIN
HALVING_INTERVAL_MAIN = 2_100_000
HALVING_INTERVAL_REGTEST = 150
SLOT_SECONDS = 60
MAX_MONEY = 21_000_000_000 * COIN

PUBKEY_ADDRESS_MAIN = 76  # X…
SCRIPT_ADDRESS_MAIN = 139  # x…
PUBKEY_ADDRESS_TEST = 140  # y…
SCRIPT_ADDRESS_TEST = 200

RPC_PORTS = {
    "main": 38442,
    "regtest": 28442,
    "test": 48442,
}
P2P_PORTS = {
    "main": 38443,
    "regtest": 28443,
    "test": 48443,
}

OP_RVN_ASSET = 0xC0
XHB1 = b"XHB1"
XVA1 = b"XVA1"
XSD1 = b"XSD1"
XID1 = b"XID1"
RVN_PREFIX = b"rvn"

BURN_ADDRESSES_MAIN = {
    "XissueAssetXXXXXXXXXXXXXXXXXXwTyxt": "issue root (removed — protocol identity only)",
    "XreissueAssetXXXXXXXXXXXXXXXZNfDqa": "reissue",
    "XissueSubAssetXXXXXXXXXXXXXXcHkFpF": "issue sub (100 XFER)",
    "XissueUniqueAssetXXXXXXXXXXXagKZDZ": "issue unique (5 XFER)",
    "XissueMsgChanneLAssetXXXXXXXcZDf2U": "issue message channel",
    "XgLobaLBurnXXXXXXXXXXXXXXXXXZTDEwo": "global burn",
}

NETWORK_LABELS = {
    "main": "mainnet",
    "test": "testnet",
    "regtest": "practice (regtest)",
}


def subsidy_at(height: int, interval: int = HALVING_INTERVAL_MAIN) -> int:
    if height < 1:
        return 0
    if interval <= 0:
        interval = HALVING_INTERVAL_MAIN
    halvings = height // interval
    if halvings >= 64:
        return 0
    return INITIAL_SUBSIDY >> halvings


def winner_count(height: int, interval: int = HALVING_INTERVAL_MAIN) -> int:
    if height < 0:
        height = 0
    if interval <= 0:
        return 1
    halvings = height // interval
    if halvings > 1023:
        halvings = 1023
    return halvings + 1


def circulating_supply(height: int, interval: int = HALVING_INTERVAL_MAIN) -> int:
    """Spendable subsidy issued from height 1 through `height` (genesis pays 0)."""
    if height < 1:
        return 0
    if interval <= 0:
        interval = HALVING_INTERVAL_MAIN
    total = 0
    for era in range(0, 64):
        start = 1 if era == 0 else era * interval
        end = min(height, (era + 1) * interval - 1)
        if start > height:
            break
        if start > end:
            continue
        amount = subsidy_at(start, interval)
        if amount == 0:
            break
        total += amount * (end - start + 1)
    return total


def slot_from_time(unix_time: int) -> int:
    if unix_time < 0:
        return 0
    return unix_time // SLOT_SECONDS


def slot_from_height(height: int, genesis_time: int = GENESIS_TIME_MAIN) -> int:
    if height <= 0:
        return slot_from_time(genesis_time)
    return slot_from_time(genesis_time) + height


def slot_start_time(height: int, genesis_time: int = GENESIS_TIME_MAIN) -> int:
    return slot_from_height(height, genesis_time) * SLOT_SECONDS


def halving_interval_for_network(network: str) -> int:
    if network == "regtest":
        return HALVING_INTERVAL_REGTEST
    return HALVING_INTERVAL_MAIN


def classify_asset_name(name: str) -> str:
    if not name:
        return "unknown"
    if name.endswith("!"):
        return "owner"
    if "#" in name:
        return "unique"
    if "/" in name:
        return "sub"
    if "~" in name:
        return "channel"
    if name.startswith("$"):
        return "restricted"
    return "root"
