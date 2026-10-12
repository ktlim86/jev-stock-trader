"""Paper allocation for buy signals. This never places an order.

Rules
-----
1. Let N be today's buy count. Each buy is offered ``portfolio_amount / N``
   in the portfolio currency (HKD for the Hang Seng).
2. Shares are the greatest whole board lot whose cost (``shares * price``)
   fits in that budget. A budget smaller than one lot buys nothing.
3. The board lot is Longbridge ``static_info.lot_size``. If that value is
   missing, ``.HK`` names use ``default_lot_size_hk`` (100) and other markets
   use ``default_lot_size`` (1). Real Hong Kong board lots are not all 100;
   the static-info value is the one to trust.
4. ``hold`` keeps the share amount already stored for that ticker, or 0 when
   there is no previous row.
5. ``sell`` sets the share amount to 0.
6. A new buy replaces that ticker's previous share amount. Holds are not
   resized, so the sum of notionals can differ from ``portfolio_amount``.
   The sheet is a paper log of the buy sleeve, not a broker balance check.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any


@dataclass
class SizedPosition:
    symbol: str
    status: str
    shares: int
    price: float | None
    total_price: float
    note: str = ""


def board_lot_shares(budget: Decimal, price: Decimal, lot_size: int) -> int:
    if budget <= 0 or price <= 0 or lot_size <= 0:
        return 0
    lot_cost = price * lot_size
    lots = (budget / lot_cost).to_integral_value(rounding=ROUND_FLOOR)
    return int(lots) * lot_size


def size_positions(
    decisions: list[dict[str, Any]],
    *,
    portfolio_amount: float,
    prices: dict[str, float | None],
    lot_sizes: dict[str, int | None],
    existing_shares: dict[str, int],
    default_lot_size_hk: int = 100,
    default_lot_size: int = 1,
) -> list[SizedPosition]:
    buys = [row for row in decisions if row.get("choice") == "buy"]
    budget = (
        Decimal(str(portfolio_amount)) / Decimal(len(buys)) if buys else Decimal(0)
    )
    sized: list[SizedPosition] = []
    for row in decisions:
        symbol = str(row["symbol"])
        choice = str(row["choice"])
        price = prices.get(symbol)
        price_dec = Decimal(str(price)) if price is not None and price > 0 else None
        note = ""
        if choice == "sell":
            shares = 0
        elif choice == "hold":
            shares = max(int(existing_shares.get(symbol, 0)), 0)
        elif choice == "buy":
            lot = _lot_size(symbol, lot_sizes.get(symbol), default_lot_size_hk, default_lot_size)
            if price_dec is None:
                shares = 0
                note = "buy signal but no positive price; share amount left at 0"
            elif lot <= 0:
                shares = 0
                note = "buy signal but board lot is unknown; share amount left at 0"
            else:
                shares = board_lot_shares(budget, price_dec, lot)
                if shares == 0:
                    note = "buy budget is smaller than one board lot"
                if lot_sizes.get(symbol) in (None, 0):
                    note = (note + "; " if note else "") + f"lot size defaulted to {lot}"
        else:
            continue
        total = Decimal(0) if price_dec is None else (price_dec * shares)
        sized.append(
            SizedPosition(
                symbol=symbol,
                status=choice,
                shares=shares,
                price=None if price_dec is None else float(price_dec),
                total_price=float(total.quantize(Decimal("0.01"))),
                note=note,
            )
        )
    return sized


def _lot_size(
    symbol: str,
    reported: int | None,
    hk_default: int,
    other_default: int,
) -> int:
    if isinstance(reported, int) and reported > 0:
        return reported
    if isinstance(reported, float) and reported > 0:
        return int(reported)
    if symbol.endswith(".HK"):
        return hk_default
    return other_default
