"""1.0.14 host/guest lottery share — wallet send, not consensus.

Matches x-coin src/hostshare.cpp GuestPot / GuestShare. A verified host
sends a percent of a mature lottery coinbase, split equally among guests.
Guests never enter the hat. There is no OP_RETURN marker, so the explorer
tags a spend only when the amounts match that wallet math.
"""

from __future__ import annotations

MIN_PERCENT = 1
MAX_PERCENT = 100
MAX_GUESTS = 16


def guest_pot(n_value: int, percent: int) -> int:
    if n_value <= 0 or percent < MIN_PERCENT:
        return 0
    if percent > MAX_PERCENT:
        percent = MAX_PERCENT
    return (n_value // 100) * percent + ((n_value % 100) * percent) // 100


def guest_share(pot: int, n_ready: int) -> int:
    if pot <= 0 or n_ready <= 0:
        return 0
    return pot // n_ready


def percent_for_pot(n_value: int, pot: int) -> int | None:
    if n_value <= 0 or pot <= 0 or pot > n_value:
        return None
    for percent in range(MIN_PERCENT, MAX_PERCENT + 1):
        if guest_pot(n_value, percent) == pot:
            return percent
    return None


def detect_host_share(
    win_amount: int,
    host_address: str | None,
    outputs: list[tuple[str | None, int]],
) -> dict | None:
    """Return share fields when outputs look like a 1.0.14 guest split."""
    guests: list[tuple[str | None, int]] = []
    for addr, amt in outputs:
        if amt <= 0:
            continue
        if host_address and addr == host_address:
            continue
        guests.append((addr, amt))
    if not guests or len(guests) > MAX_GUESTS:
        return None
    amounts = {amt for _addr, amt in guests}
    if len(amounts) != 1:
        return None
    each = guests[0][1]
    pot = each * len(guests)
    if guest_share(pot, len(guests)) != each:
        return None
    percent = percent_for_pot(win_amount, pot)
    if percent is None:
        return None
    return {
        "percent": percent,
        "pot": pot,
        "guest_each": each,
        "guests": [{"address": addr, "amount": amt} for addr, amt in guests],
    }
