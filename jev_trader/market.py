"""Read-only Longbridge market data.

``MarketContext.constituent(symbol)`` calls ``GET /v1/quote/index-constituents``
with only ``symbol``. The Python SDK does not take a page or size argument, and
the typed response has no cursor. A full index therefore comes back in one
``stocks`` list when the server sends one.

The Longbridge MCP tool did not do that for HSI.HK: the saved payload said
``total`` 95 and included 20 stocks. This client reads the raw JSON as well. If
``total`` is larger than the list, it retries with ``limit=0`` and then with
``page`` / ``size`` until the symbols cover ``total``. If those parameters do
not reveal more names, it raises ``ConstituentTruncatedError`` instead of
silently deciding on the first page.

This module never constructs a trade, asset, or portfolio context and never
submits an order.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from jev_trader.config import DatasetConfig
from jev_trader.serialize import to_plain

logger = logging.getLogger(__name__)

STATE_KEYS = {
    "financial_report": "financial_report",
    "institution_rating": "institution_rating",
    "forecast_eps": "forecast_eps",
    "valuation_comparison": "valuation_comparison",
    "shareholder": "shareholder",
    "insider_trades": "shareholder_top",
    "kline": "kline_1y",
}

_KIND = {
    "ALL": "All",
    "all": "All",
    "All": "All",
    "IS": "IncomeStatement",
    "IncomeStatement": "IncomeStatement",
    "BS": "BalanceSheet",
    "BalanceSheet": "BalanceSheet",
    "CF": "CashFlow",
    "CashFlow": "CashFlow",
}
_PERIOD = {
    "af": "Annual",
    "annual": "Annual",
    "Annual": "Annual",
    "saf": "SemiAnnual",
    "SemiAnnual": "SemiAnnual",
    "q1": "Q1",
    "Q1": "Q1",
    "q2": "Q2",
    "Q2": "Q2",
    "q3": "Q3",
    "Q3": "Q3",
    "qf": "QuarterlyFull",
    "QuarterlyFull": "QuarterlyFull",
    "ThreeQ": "ThreeQ",
}


class ConstituentTruncatedError(RuntimeError):
    """The index payload reported more members than the client could collect."""


class LongbridgeMarketData:
    def __init__(
        self,
        market: Any,
        fundamental: Any,
        quote: Any,
        http: Any = None,
        pause_seconds: float = 0.0,
        sleep: Any = time.sleep,
    ) -> None:
        self.market = market
        self.fundamental = fundamental
        self.quote = quote
        self.http = http
        self.pause_seconds = pause_seconds
        self._sleep = sleep

    def constituents(self, index_symbol: str) -> list[dict[str, Any]]:
        stocks = _dedupe(self._from_sdk(index_symbol))
        total: int | None = None
        if self.http is not None:
            try:
                raw_stocks, total = self._raw_page(index_symbol, "")
            except Exception as exc:
                logger.warning("constituent raw probe failed for %s: %s", index_symbol, exc)
                raw_stocks = []
            if len(raw_stocks) > len(stocks):
                stocks = _dedupe(raw_stocks)
        if isinstance(total, int) and total > len(stocks):
            stocks = self._complete_pages(index_symbol, stocks, total)
        if isinstance(total, int) and len(stocks) < total:
            raise ConstituentTruncatedError(_truncation_message(index_symbol, total, len(stocks)))
        return stocks

    def static_refs(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(symbols), 100):
            chunk = symbols[start : start + 100]
            self._pause()
            try:
                response = self.quote.static_info(chunk)
            except Exception as exc:
                logger.warning("static_info failed for %s names: %s", len(chunk), exc)
                continue
            for row in _as_list(response, ("secu_static_info", "list")):
                plain = row if isinstance(row, dict) else to_plain(row)
                if not isinstance(plain, dict):
                    continue
                symbol = plain.get("symbol")
                if not symbol:
                    continue
                found[str(symbol)] = {
                    "lot_size": plain.get("lot_size"),
                    "currency": plain.get("currency"),
                    "name": plain.get("name_en") or plain.get("name"),
                }
        return found

    def load(
        self,
        symbol: str,
        datasets: list[tuple[str, DatasetConfig]],
        currency: str,
    ) -> tuple[dict[str, Any], list[str]]:
        raw: dict[str, Any] = {}
        errors: list[str] = []
        for name, dataset in datasets:
            self._pause()
            try:
                raw[STATE_KEYS[name]] = self._fetch(name, symbol, dataset.options, currency)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                logger.warning("%s %s failed: %s", symbol, name, exc)
        return raw, errors

    def _fetch(self, name: str, symbol: str, options: dict[str, Any], currency: str) -> Any:
        if name == "financial_report":
            return self._financial_report(symbol, options)
        if name == "institution_rating":
            return to_plain(self.fundamental.institution_rating(symbol))
        if name == "forecast_eps":
            return to_plain(self.fundamental.forecast_eps(symbol))
        if name == "valuation_comparison":
            code = str(options.get("currency") or currency or "HKD")
            peers = options.get("comparison_symbols")
            return to_plain(self.fundamental.valuation_comparison(symbol, code, peers))
        if name == "shareholder":
            return to_plain(self.fundamental.shareholder(symbol))
        if name == "insider_trades":
            # No insider-trade feed. Top holders include institutions and insiders,
            # and ``shares_changed`` is the holding change for the period.
            return to_plain(self.fundamental.shareholder_top(symbol))
        if name == "kline":
            return self._kline(symbol, options)
        raise KeyError(name)

    def _financial_report(self, symbol: str, options: dict[str, Any]) -> Any:
        from longbridge.openapi import FinancialReportKind, FinancialReportPeriod

        kind_name = _KIND.get(str(options.get("kind", "All")), "All")
        period_token = options.get("period", options.get("report_type", "Annual"))
        period_name = _PERIOD.get(str(period_token), "Annual")
        response = self.fundamental.financial_report(
            symbol,
            kind=getattr(FinancialReportKind, kind_name),
            period=getattr(FinancialReportPeriod, period_name),
        )
        return to_plain(response)

    def _kline(self, symbol: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        from longbridge.openapi import AdjustType, Period, TradeSessions

        lookback = int(options.get("lookback_days", 365))
        end = date.today()
        start = end - timedelta(days=lookback)
        forward = bool(options.get("forward_adjust", True))
        adjust = AdjustType.ForwardAdjust if forward else AdjustType.NoAdjust
        session_name = str(options.get("trade_sessions", "intraday")).lower()
        session = TradeSessions.Intraday if session_name == "intraday" else TradeSessions.All
        collected: list[dict[str, Any]] = []
        seen: set[Any] = set()
        cursor_end = end
        for _ in range(6):
            response = self.quote.history_candlesticks_by_date(
                symbol,
                Period.Day,
                adjust,
                start=start,
                end=cursor_end,
                trade_sessions=session,
            )
            chunk = _as_list(response, ("candlesticks",))
            if not chunk:
                break
            plains = []
            for bar in chunk:
                plain = bar if isinstance(bar, dict) else to_plain(bar)
                if not isinstance(plain, dict):
                    continue
                stamp = plain.get("timestamp")
                if stamp in seen:
                    continue
                seen.add(stamp)
                plains.append(plain)
            collected.extend(plains)
            if len(chunk) < 1000:
                break
            oldest = _oldest_day(plains)
            if oldest is None or oldest <= start:
                break
            nxt = oldest - timedelta(days=1)
            if nxt >= cursor_end:
                break
            cursor_end = nxt
            self._pause()
        collected.sort(key=lambda bar: _stamp_sort(bar.get("timestamp")))
        return collected

    def _from_sdk(self, index_symbol: str) -> list[dict[str, Any]]:
        response = self.market.constituent(index_symbol)
        plain = to_plain(response)
        rows = []
        if isinstance(plain, dict):
            rows = plain.get("stocks") or []
        return [_normalize_stock(row) for row in rows if isinstance(row, dict)]

    def _raw_page(self, index_symbol: str, extra: str) -> tuple[list[dict[str, Any]], int | None]:
        path = f"/v1/quote/index-constituents?symbol={quote(index_symbol)}"
        if extra:
            path = f"{path}&{extra}"
        body = self.http.request("GET", path)
        data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
        if not isinstance(data, dict):
            data = {}
        rows = data.get("stocks") or data.get("constituents") or []
        if not isinstance(rows, list):
            rows = []
        total = data.get("total")
        if total is None and isinstance(body, dict):
            total = body.get("total")
        total_int = int(total) if isinstance(total, int) or (isinstance(total, str) and str(total).isdigit()) else None
        return [_normalize_stock(row) for row in rows if isinstance(row, dict)], total_int

    def _complete_pages(
        self, index_symbol: str, stocks: list[dict[str, Any]], total: int
    ) -> list[dict[str, Any]]:
        seen = {row["symbol"]: row for row in stocks if row.get("symbol")}
        probes = [
            "limit=0",
            f"page=1&size={total}",
            f"page=1&page_size={total}",
        ]
        for extra in probes:
            self._pause()
            try:
                batch, _ = self._raw_page(index_symbol, extra)
            except Exception as exc:
                logger.warning("constituent probe %s failed: %s", extra, exc)
                continue
            for row in batch:
                symbol = row.get("symbol")
                if symbol and symbol not in seen:
                    seen[symbol] = row
            if len(seen) >= total:
                return list(seen.values())
        stagnant = 0
        for page in range(2, 31):
            self._pause()
            before = len(seen)
            try:
                batch, _ = self._raw_page(
                    index_symbol, f"page={page}&size={max(len(stocks), 50)}"
                )
            except Exception as exc:
                logger.warning("constituent page %s failed: %s", page, exc)
                break
            for row in batch:
                symbol = row.get("symbol")
                if symbol and symbol not in seen:
                    seen[symbol] = row
            if len(seen) >= total:
                return list(seen.values())
            if len(seen) == before:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
        if len(seen) < total:
            raise ConstituentTruncatedError(
                _truncation_message(index_symbol, total, len(seen))
            )
        return list(seen.values())

    def _pause(self) -> None:
        if self.pause_seconds > 0:
            self._sleep(self.pause_seconds)


def connect(pause_seconds: float = 0.0) -> LongbridgeMarketData:
    """Open read-only contexts from ``LONGBRIDGE_APP_KEY``, ``LONGBRIDGE_APP_SECRET``, and ``LONGBRIDGE_ACCESS_TOKEN``."""
    import os

    os.environ.setdefault("LONGBRIDGE_LANGUAGE", "en")
    os.environ.setdefault("LONGBRIDGE_PRINT_QUOTE_PACKAGES", "false")
    missing = [
        name
        for name in (
            "LONGBRIDGE_APP_KEY",
            "LONGBRIDGE_APP_SECRET",
            "LONGBRIDGE_ACCESS_TOKEN",
        )
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise RuntimeError(
            "Missing Longbridge read-only credentials: "
            + ", ".join(missing)
            + ". Set them in the environment or in .env. Do not use a trade token for anything else; this program only opens market, fundamental, and quote contexts."
        )
    from longbridge.openapi import Config, FundamentalContext, HttpClient, MarketContext, QuoteContext

    cfg = Config.from_apikey_env()
    return LongbridgeMarketData(
        market=MarketContext(cfg),
        fundamental=FundamentalContext(cfg),
        quote=QuoteContext(cfg),
        http=HttpClient.from_apikey_env(),
        pause_seconds=pause_seconds,
    )


def _truncation_message(index_symbol: str, total: int, got: int) -> str:
    return (
        f"{index_symbol}: the constituents payload says total={total} but only {got} "
        "distinct symbols were returned. MarketContext.constituent accepts only the "
        "index symbol (GET /v1/quote/index-constituents?symbol=...) and does not page. "
        "The MCP sample for HSI.HK was 20 of 95. Extra limit=0 and page/size requests "
        "did not fill the gap, so this run stops rather than scoring a partial index."
    )


def _normalize_stock(row: dict[str, Any]) -> dict[str, Any]:
    symbol = row.get("symbol") or row.get("code")
    return {
        "symbol": str(symbol) if symbol else "",
        "name": row.get("name") or "",
        "last_done": row.get("last_done"),
        "currency": row.get("currency"),
    }


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = row.get("symbol")
        if symbol and symbol not in seen:
            seen[symbol] = row
    return list(seen.values())


def _as_list(response: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(response, list):
        return response
    plain = response if isinstance(response, dict) else to_plain(response)
    if isinstance(plain, list):
        return plain
    if isinstance(plain, dict):
        for key in keys:
            value = plain.get(key)
            if isinstance(value, list):
                return value
        return []
    return []


def _stamp_sort(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return 0


def _oldest_day(bars: list[dict[str, Any]]) -> date | None:
    stamps = [_stamp_sort(bar.get("timestamp")) for bar in bars]
    stamps = [stamp for stamp in stamps if stamp > 0]
    if not stamps:
        return None
    return datetime.fromtimestamp(min(stamps), tz=timezone.utc).date()
