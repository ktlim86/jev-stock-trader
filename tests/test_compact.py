import math

from jev_trader.compact import build_state, compact_kline, englishize
from jev_trader.config import CompactConfig


def _rsi(closes):
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    recent = returns[-14:]
    gains = sum(max(item, 0) for item in recent)
    losses = sum(-min(item, 0) for item in recent)
    return round(100 - 100 / (1 + (gains / max(losses, 1e-9))), 1)


def test_kline_indicators_follow_the_stage1_definitions():
    closes = [float(i) for i in range(1, 221)]
    bars = [
        {
            "close": price,
            "high": price + 1,
            "low": price - 0.5,
            "volume": 1000 + index,
            "timestamp": index,
        }
        for index, price in enumerate(closes)
    ]
    out = compact_kline(bars, weekly_closes=26)
    assert out["bars"] == 220
    assert out["last_close"] == 220
    assert out["ret_1m"] == round(220 / 200 - 1, 4)
    assert out["ret_3m"] == round(220 / 158 - 1, 4)
    assert out["ret_6m"] == round(220 / 95 - 1, 4)
    assert out["ret_1y"] == round(220 / 1 - 1, 4)
    assert out["sma20"] == round(sum(closes[-20:]) / 20, 3)
    assert out["sma200"] == round(sum(closes[-200:]) / 200, 3)
    assert out["rsi14"] == _rsi(closes)
    assert out["high_52w"] == max(price + 1 for price in closes)
    assert out["low_52w"] == min(price - 0.5 for price in closes)
    assert out["avg_vol_20d"] == round(sum(1000 + index for index in range(200, 220)) / 20)
    assert len(out["weekly_closes"]) == 26
    assert out["max_dd_1y"] == 0
    assert math.isfinite(out["vol_ann"])


def test_drawdown_uses_the_running_peak():
    bars = [
        {"close": 10, "high": 10, "low": 10, "volume": 1, "timestamp": 1},
        {"close": 8, "high": 9, "low": 7, "volume": 1, "timestamp": 2},
        {"close": 9, "high": 9, "low": 8, "volume": 1, "timestamp": 3},
    ]
    # Pad so RSI has 14 returns. The drawdown is still set by the dip to 8.
    for index in range(4, 20):
        bars.append({"close": 9, "high": 9, "low": 9, "volume": 1, "timestamp": index})
    out = compact_kline(bars)
    assert out["max_dd_1y"] == round(8 / 10 - 1, 4)
    assert out["low_52w"] == 7


def test_chinese_labels_are_translated_or_dropped_and_latest_periods_kept():
    raw = {
        "financial_report": {
            "list": {
                "b_s": {
                    "indicators": [
                        {
                            "title": "资产与负债",
                            "tip": "当营业利润和经营现金流两者都≤0时，此时该指标无意义，显示--。",
                            "name_locales": {"zh-CN": "资产"},
                            "field": "total_assets",
                            "accounts": [
                                {
                                    "name": "总资产(HKD)",
                                    "values": [
                                        {"year": year, "period": f"FY {year}", "value": str(year)}
                                        for year in range(2016, 2026)
                                    ],
                                }
                            ],
                        }
                    ]
                }
            },
            "empty": "",
        },
        "forecast_eps": {"items": [{"forecast_eps_mean": str(i)} for i in range(8)]},
        "shareholder_top": {
            "info": [
                {"period": "最新", "share_holders": [{"name": "A"}]},
                {"period": "Q2 2026", "share_holders": [{"name": "B"}]},
                {"period": "Q1 2026", "share_holders": [{"name": "C"}]},
            ]
        },
        "valuation_comparison": {
            "list": [{"symbol": "700.HK", "name": "腾讯", "history": [{"pe": str(i)} for i in range(20)]}]
        },
        "institution_rating": {"analyst": {"industry_name": "互联网内容与信息", "industry_id": "9"}},
    }
    state = build_state(raw, CompactConfig(), symbol="700.HK", name="腾讯控股", currency="HKD")
    assert state["symbol"] == "700.HK"
    assert "name" not in state  # untranslated Chinese name is dropped
    assert state["currency"] == "HKD"
    indicator = state["financial_report"]["list"]["b_s"]["indicators"][0]
    assert indicator["title"] == "Assets and liabilities"
    assert "tip" not in indicator
    assert "name_locales" not in indicator
    assert indicator["field"] == "total_assets"
    values = indicator["accounts"][0]["values"]
    assert [row["year"] for row in values] == [2021, 2022, 2023, 2024, 2025]
    assert indicator["accounts"][0]["name"] == "Total assets (HKD)"
    assert "empty" not in state["financial_report"]
    assert len(state["forecast_eps"]["items"]) == 6
    assert state["forecast_eps"]["items"][0]["forecast_eps_mean"] == "2"
    periods = state["shareholder_top"]["info"]
    assert [row["period"] for row in periods] == ["Latest", "Q2 2026"]
    assert len(state["valuation_comparison"]["list"][0]["history"]) == 12
    assert "name" not in state["valuation_comparison"]["list"][0]
    assert state["institution_rating"]["analyst"]["industry_name"] == "Internet content and information"
    assert "industry_id" not in state["institution_rating"]["analyst"]


def test_englishize_leaves_ascii_and_drops_unknown_chinese():
    assert englishize("BlackRock, Inc.") == "BlackRock, Inc."
    assert englishize("最新") == "Latest"
    assert englishize("这里是一段没有词条的说明文字") is None
