from decimal import Decimal

from jev_trader.portfolio import board_lot_shares, size_positions


def test_board_lot_rounds_down():
    assert board_lot_shares(Decimal("100000"), Decimal("600"), 100) == 100
    assert board_lot_shares(Decimal("50000"), Decimal("600"), 100) == 0
    assert board_lot_shares(Decimal("100000"), Decimal("0"), 100) == 0


def test_equal_weight_buys_hold_keeps_shares_and_sell_flattens():
    decisions = [
        {"symbol": "700.HK", "choice": "buy"},
        {"symbol": "5.HK", "choice": "buy"},
        {"symbol": "388.HK", "choice": "hold"},
        {"symbol": "1.HK", "choice": "sell"},
    ]
    sized = size_positions(
        decisions,
        portfolio_amount=100_000,
        prices={"700.HK": 400, "5.HK": 50, "388.HK": 10, "1.HK": 20},
        lot_sizes={"700.HK": 100, "5.HK": 400, "388.HK": 100, "1.HK": 100},
        existing_shares={"388.HK": 250, "1.HK": 80, "700.HK": 999},
    )
    by_symbol = {row.symbol: row for row in sized}
    # 100000 / 2 = 50000. 700.HK lot cost is 400*100=40000, so one lot.
    assert by_symbol["700.HK"].shares == 100
    assert by_symbol["700.HK"].total_price == 40000
    # 5.HK lot cost is 50*400=20000, so two lots.
    assert by_symbol["5.HK"].shares == 800
    assert by_symbol["5.HK"].total_price == 40000
    assert by_symbol["388.HK"].shares == 250
    assert by_symbol["388.HK"].total_price == 2500
    assert by_symbol["1.HK"].shares == 0
    assert by_symbol["1.HK"].total_price == 0


def test_missing_price_or_tiny_budget_buys_nothing():
    sized = size_positions(
        [{"symbol": "700.HK", "choice": "buy"}, {"symbol": "5.HK", "choice": "buy"}],
        portfolio_amount=1000,
        prices={"700.HK": None, "5.HK": 500},
        lot_sizes={"5.HK": 100},
        existing_shares={},
    )
    by_symbol = {row.symbol: row for row in sized}
    assert by_symbol["700.HK"].shares == 0
    assert "no positive price" in by_symbol["700.HK"].note
    assert by_symbol["5.HK"].shares == 0
    assert "board lot" in by_symbol["5.HK"].note
