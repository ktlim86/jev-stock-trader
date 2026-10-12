"""Shrink a ticker's raw datasets so state plus the choice question fit in 32k tokens.

The stage-1 estimator turned a year of daily bars into indicators, kept the
latest shareholder periods and EPS forecasts, and deleted empty keys. This
module does that, and also translates known Chinese labels to English. A
Chinese string that is not in the glossary is dropped, because Jev is called
with an English question and the leftover text would spend the token budget
without being a stable field name.
"""

from __future__ import annotations

import math
import re
import statistics
from typing import Any

# Keys the stage-1 script removed, plus verbose locale and tooltip fields.
# ``field`` is kept: it is the ASCII line-item code and survives when a
# Chinese display name is dropped.
DROP_KEYS = {
    "name_locales",
    "stocks",
    "object_id",
    "ranking_code",
    "industry_ranking",
    "fp_end",
    "has_yoy",
    "percent",
    "tip",
    "intro",
    "name_cn",
    "name_hk",
    "forward_url",
    "industry_id",
}

_CJK = re.compile(r"[\u4e00-\u9fff]")

# Short labels seen on Hang Seng financial statements and holder periods.
# Longer keys are applied first when a value is not an exact match.
GLOSSARY = {
    "资产与负债": "Assets and liabilities",
    "总资产(HKD)": "Total assets (HKD)",
    "总负债(HKD)": "Total liabilities (HKD)",
    "权益乘数": "Equity multiplier",
    "每股净资产(HKD)": "Book value per share (HKD)",
    "每股净资产": "Book value per share",
    "每股收益(HKD)": "Earnings per share (HKD)",
    "每股收益": "Earnings per share",
    "营业收入(HKD)": "Revenue (HKD)",
    "营业收入": "Revenue",
    "营业利润(HKD)": "Operating profit (HKD)",
    "营业利润": "Operating profit",
    "净利润(HKD)": "Net profit (HKD)",
    "净利润": "Net profit",
    "毛利率": "Gross margin",
    "净利率": "Net margin",
    "经营现金流(HKD)": "Operating cash flow (HKD)",
    "经营现金流": "Operating cash flow",
    "投资现金流(HKD)": "Investing cash flow (HKD)",
    "投资现金流": "Investing cash flow",
    "融资现金流(HKD)": "Financing cash flow (HKD)",
    "融资现金流": "Financing cash flow",
    "自由现金流(HKD)": "Free cash flow (HKD)",
    "自由现金流": "Free cash flow",
    "资本支出(HKD)": "Capital expenditure (HKD)",
    "资本支出": "Capital expenditure",
    "举债与偿债": "Borrowing and repayment",
    "举债总额(HKD)": "Total borrowing (HKD)",
    "偿债总额(HKD)": "Total debt repayment (HKD)",
    "净债务(HKD)": "Net debt (HKD)",
    "净债务": "Net debt",
    "现金及短期投资(HKD)及占比": "Cash and short-term investments (HKD) and share of assets",
    "现金及短投": "Cash and short-term investments",
    "存货与应收": "Inventory and receivables",
    "存货(HKD)及占比": "Inventory (HKD) and share of assets",
    "应收(HKD)及占比": "Receivables (HKD) and share of assets",
    "固定资产净值(HKD)及占比": "Net fixed assets (HKD) and share of assets",
    "长期投资(HKD)及占比": "Long-term investments (HKD) and share of assets",
    "长期投资": "Long-term investments",
    "资产周转率": "Asset turnover",
    "利润含金量": "Earnings quality",
    "现金流充裕率": "Cash flow adequacy",
    "营业利润/经营现金流": "Operating profit / operating cash flow",
    "经营现金流/(资本支出+分红+偿债）": "Operating cash flow / (capex + dividends + debt repayment)",
    "互联网内容与信息": "Internet content and information",
    "最新": "Latest",
}

_FRAGMENTS = sorted(GLOSSARY.items(), key=lambda item: len(item[0]), reverse=True)


def englishize(text: str) -> str | None:
    """Return an English label, the original ASCII text, or None when Chinese remains."""
    if text in GLOSSARY:
        return GLOSSARY[text]
    if not _CJK.search(text):
        return text
    translated = text
    for zh, en in _FRAGMENTS:
        if zh in translated:
            translated = translated.replace(zh, en)
    if _CJK.search(translated):
        return None
    return translated


def _empty(value: Any) -> bool:
    return value in ("", None, [], {})


def clean(value: Any, drop: set[str] = DROP_KEYS) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in drop:
                continue
            cleaned = clean(item, drop)
            if _empty(cleaned):
                continue
            out[str(key)] = cleaned
        return out
    if isinstance(value, list):
        out_list = []
        for item in value:
            cleaned = clean(item, drop)
            if _empty(cleaned):
                continue
            out_list.append(cleaned)
        return out_list
    if isinstance(value, str):
        return englishize(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _as_float(value: Any) -> float | None:
    if value is None or value is True or value is False:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if not math.isfinite(number):
        return None
    return number


def _year_of(point: dict[str, Any], index: int) -> int:
    year = point.get("year")
    if isinstance(year, int):
        return year
    if isinstance(year, str) and year.isdigit():
        return int(year)
    period = str(point.get("period") or "")
    digits = re.findall(r"20\d{2}|19\d{2}", period)
    if digits:
        return int(digits[-1])
    # No year: keep the API order (newest first) ahead of older rows.
    return 10_000 - index


def _latest_values(points: list[Any], keep: int) -> list[Any]:
    dicts = [point for point in points if isinstance(point, dict)]
    if len(dicts) != len(points):
        return points[:keep]
    ranked = sorted(
        enumerate(dicts),
        key=lambda item: _year_of(item[1], item[0]),
        reverse=True,
    )
    chosen = sorted(ranked[:keep], key=lambda item: item[0])
    return [point for _, point in chosen]


def trim_financial(payload: Any, periods: int | None) -> Any:
    if periods is None:
        return payload

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key == "values" and isinstance(value, list):
                    out[key] = _latest_values(value, periods)
                else:
                    out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(payload)


def trim_forecast(payload: Any, keep: int) -> Any:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return payload
    return {**payload, "items": payload["items"][-keep:]}


def trim_valuation(payload: Any, keep: int | None) -> Any:
    if keep is None:
        return payload

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key == "history" and isinstance(value, list):
                    out[key] = value[-keep:]
                else:
                    out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(payload)


def _holder_periods(payload: Any) -> tuple[dict[str, Any], str, list[Any]] | None:
    if isinstance(payload, dict):
        for key in ("info", "data", "list"):
            value = payload.get(key)
            if isinstance(value, list) and _looks_like_periods(value):
                return payload, key, value
            if isinstance(value, dict):
                nested = _holder_periods(value)
                if nested is not None:
                    parent, child_key, periods = nested
                    return parent, child_key, periods
    return None


def _looks_like_periods(value: list[Any]) -> bool:
    if not value or not isinstance(value[0], dict):
        return False
    sample = value[0]
    return any(key in sample for key in ("share_holders", "shareholders", "period", "holders"))


def trim_shareholder_top(payload: Any, keep: int) -> Any:
    found = _holder_periods(payload)
    if found is None or not isinstance(payload, dict):
        return payload
    parent, key, periods = found
    parent[key] = periods[:keep]
    return payload


def compact_kline(bars: Any, weekly_closes: int = 26) -> dict[str, Any] | None:
    """Match the stage-1 indicator set, with 52-week high and low taken from the bar range."""
    if isinstance(bars, dict) and "last_close" in bars:
        return bars
    if not isinstance(bars, list) or not bars:
        return None
    rows: list[dict[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        close = _as_float(bar.get("close"))
        if close is None:
            continue
        high = _as_float(bar.get("high"))
        low = _as_float(bar.get("low"))
        volume = _as_float(bar.get("volume")) or 0.0
        rows.append(
            {
                "close": close,
                "high": high if high is not None else close,
                "low": low if low is not None else close,
                "volume": volume,
                "timestamp": bar.get("timestamp") or 0,
            }
        )
    if len(rows) < 2:
        return None
    rows.sort(key=lambda row: row["timestamp"] or 0)
    closes = [row["close"] for row in rows]
    volumes = [row["volume"] for row in rows]
    highs = [row["high"] for row in rows]
    lows = [row["low"] for row in rows]

    def ratio(periods_back: int) -> float | None:
        if len(closes) <= periods_back:
            return None
        base = closes[-1 - periods_back]
        if base == 0:
            return None
        return round(closes[-1] / base - 1, 4)

    def sma(window: int) -> float | None:
        if len(closes) < window:
            return None
        return round(sum(closes[-window:]) / window, 3)

    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
    indicators: dict[str, Any] = {
        "bars": len(closes),
        "last_close": closes[-1],
        "ret_1m": ratio(20),
        "ret_3m": ratio(62),
        "ret_6m": ratio(125),
        "ret_1y": round(closes[-1] / closes[0] - 1, 4) if closes[0] else None,
        "sma20": sma(20),
        "sma50": sma(50),
        "sma200": sma(200),
        "high_52w": max(highs),
        "low_52w": min(lows),
    }
    if len(returns) >= 14:
        recent = returns[-14:]
        gains = [max(item, 0) for item in recent]
        losses = [-min(item, 0) for item in recent]
        indicators["rsi14"] = round(100 - 100 / (1 + (sum(gains) / max(sum(losses), 1e-9))), 1)
    if len(returns) >= 2:
        indicators["vol_ann"] = round(statistics.pstdev(returns) * math.sqrt(252), 4)
    peak = closes[0]
    max_dd = 0.0
    for price in closes:
        peak = max(peak, price)
        if peak:
            max_dd = min(max_dd, price / peak - 1)
    indicators["max_dd_1y"] = round(max_dd, 4)
    if len(volumes) >= 20:
        indicators["avg_vol_20d"] = round(sum(volumes[-20:]) / 20)
    window = max(weekly_closes, 1) * 5
    step = closes[-window::5]
    indicators["weekly_closes"] = [round(price, 2) for price in step[-weekly_closes:]]
    return {key: value for key, value in indicators.items() if not _empty(value)}


def compact_datasets(raw: dict[str, Any], settings: Any) -> dict[str, Any]:
    """Compact one ticker. ``settings`` is a ``CompactConfig`` or any object with the same fields."""
    prepared: dict[str, Any] = {}
    if "financial_report" in raw:
        prepared["financial_report"] = trim_financial(
            raw["financial_report"], settings.financial_periods
        )
    if "institution_rating" in raw:
        prepared["institution_rating"] = raw["institution_rating"]
    if "forecast_eps" in raw:
        prepared["forecast_eps"] = trim_forecast(raw["forecast_eps"], settings.forecast_items)
    if "valuation_comparison" in raw:
        prepared["valuation_comparison"] = trim_valuation(
            raw["valuation_comparison"], settings.valuation_history_points
        )
    if "shareholder" in raw:
        prepared["shareholder"] = raw["shareholder"]
    if "shareholder_top" in raw:
        prepared["shareholder_top"] = trim_shareholder_top(
            raw["shareholder_top"], settings.shareholder_periods
        )
    if "kline_1y" in raw:
        kline = compact_kline(raw["kline_1y"], settings.weekly_closes)
        if kline is not None:
            prepared["kline_1y"] = kline
    for key, value in raw.items():
        if key not in prepared and key not in {
            "financial_report",
            "institution_rating",
            "forecast_eps",
            "valuation_comparison",
            "shareholder",
            "shareholder_top",
            "kline_1y",
        }:
            prepared[key] = value
    cleaned = clean(prepared)
    return cleaned if isinstance(cleaned, dict) else {}


def build_state(
    raw: dict[str, Any],
    settings: Any,
    *,
    symbol: str,
    name: str | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {"symbol": symbol}
    if name:
        translated = englishize(name)
        if translated:
            state["name"] = translated
    if currency:
        state["currency"] = currency
    state.update(compact_datasets(raw, settings))
    cleaned = clean(state)
    return cleaned if isinstance(cleaned, dict) else {"symbol": symbol}
