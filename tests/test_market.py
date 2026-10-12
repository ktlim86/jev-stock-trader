from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from jev_trader.config import DatasetConfig
from jev_trader.market import (
    ConstituentTruncatedError,
    LongbridgeMarketData,
    connect,
)


class Market:
    def __init__(self, stocks):
        self.stocks = stocks

    def constituent(self, symbol):
        return SimpleNamespace(
            stocks=[SimpleNamespace(**row) for row in self.stocks],
            rise_num=0,
            fall_num=0,
            flat_num=0,
        )


class Http:
    def __init__(self, routes):
        self.routes = routes
        self.paths = []

    def request(self, method, path, headers=None, body=None):
        self.paths.append(path)
        for needle, payload in self.routes:
            if needle in path:
                return payload
        raise AssertionError(path)


def _client(stocks, routes, http=True):
    return LongbridgeMarketData(
        market=Market(stocks),
        fundamental=SimpleNamespace(),
        quote=SimpleNamespace(),
        http=Http(routes) if http else None,
        pause_seconds=0,
    )


def test_sdk_list_is_used_when_it_already_covers_total():
    stocks = [
        {"symbol": "1.HK", "name": "A", "last_done": "1"},
        {"symbol": "2.HK", "name": "B", "last_done": "2"},
    ]
    client = _client(
        stocks,
        [("symbol=", {"data": {"total": 2, "stocks": stocks}})],
    )
    found = client.constituents("HSI.HK")
    assert [row["symbol"] for row in found] == ["1.HK", "2.HK"]
    assert len(client.http.paths) == 1
    assert "page=" not in client.http.paths[0]


def test_pages_when_total_is_larger_than_the_first_page():
    first = [{"symbol": "1.HK", "name": "A", "last_done": "1"}]
    client = _client(
        first,
        [
            ("limit=0", {"data": {"total": 3, "stocks": first}}),
            ("page=1&size=3", {"data": {"total": 3, "stocks": first}}),
            ("page=1&page_size=3", {"data": {"total": 3, "stocks": first}}),
            ("page=2", {"data": {"total": 3, "stocks": [
                {"symbol": "2.HK", "name": "B", "last_done": "2"},
                {"symbol": "3.HK", "name": "C", "last_done": "3"},
            ]}}),
            ("symbol=", {"data": {"total": 3, "stocks": first}}),
        ],
    )
    found = client.constituents("HSI.HK")
    assert [row["symbol"] for row in found] == ["1.HK", "2.HK", "3.HK"]


def test_truncation_is_an_error_when_paging_does_not_grow():
    first = [{"symbol": "1.HK", "name": "A", "last_done": "1"}]
    client = _client(first, [("", {"data": {"total": 95, "stocks": first}})])
    with pytest.raises(ConstituentTruncatedError, match="20 of 95"):
        client.constituents("HSI.HK")


def test_http_is_optional_when_the_sdk_returns_the_list():
    client = _client([{"symbol": "700.HK", "name": "Tencent", "last_done": "400"}], [], http=False)
    assert client.constituents("HSI.HK")[0]["symbol"] == "700.HK"


def test_financials_and_holders_use_read_only_fundamental_methods():
    calls = {}

    class Fundamental:
        def financial_report(self, symbol, kind=None, period=None):
            calls["financial"] = (symbol, str(kind), str(period))
            return {"list": {}}

        def shareholder_top(self, symbol):
            calls["top"] = symbol
            return {"info": []}

        def institution_rating(self, symbol):
            raise RuntimeError("ratings down")

    client = LongbridgeMarketData(
        market=SimpleNamespace(),
        fundamental=Fundamental(),
        quote=SimpleNamespace(),
        http=None,
    )
    raw, errors = client.load(
        "700.HK",
        [
            ("financial_report", DatasetConfig(True, {"kind": "ALL", "report_type": "af"})),
            ("insider_trades", DatasetConfig(True, {})),
            ("institution_rating", DatasetConfig(True, {})),
        ],
        "HKD",
    )
    assert calls["financial"][0] == "700.HK"
    assert "All" in calls["financial"][1]
    assert "Annual" in calls["financial"][2]
    assert calls["top"] == "700.HK"
    assert "shareholder_top" in raw
    assert any(item.startswith("institution_rating:") for item in errors)


def test_kline_requests_about_one_year_of_forward_adjusted_days():
    seen = {}

    class Quote:
        def history_candlesticks_by_date(self, symbol, period, adjust_type, start=None, end=None, trade_sessions=None):
            seen["args"] = (symbol, str(period), str(adjust_type), start, end, str(trade_sessions))
            return [
                SimpleNamespace(close="10", high="11", low="9", volume=5, timestamp=10, open="10", turnover="1", trade_session=0),
                SimpleNamespace(close="12", high="13", low="11", volume=6, timestamp=20, open="11", turnover="1", trade_session=0),
            ]

    client = LongbridgeMarketData(SimpleNamespace(), SimpleNamespace(), Quote(), http=None)
    bars = client._kline("700.HK", {"lookback_days": 365, "forward_adjust": True, "trade_sessions": "intraday"})
    symbol, period, adjust, start, end, session = seen["args"]
    assert symbol == "700.HK"
    assert "Day" in period
    assert "ForwardAdjust" in adjust
    assert "Intraday" in session
    assert end == date.today()
    assert start == end - timedelta(days=365)
    assert [bar["close"] for bar in bars] == ["10", "12"]


def test_package_never_mentions_trade_or_account_clients():
    from pathlib import Path

    banned = ("TradeContext", "submit_order", "account_balance", "AssetContext", "PortfolioContext")
    root = Path(__file__).resolve().parents[1] / "jev_trader"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, f"{path.name} contains {word}"


def test_connect_refuses_to_start_without_market_data_credentials(monkeypatch):
    for name in ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="LONGBRIDGE_APP_KEY"):
        connect()
