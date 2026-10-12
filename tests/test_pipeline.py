from jev_trader.config import config_from_dict
from jev_trader.pipeline import collect, estimate, execute, load_state_dir
from jev_trader.sheets import POSITION_HEADERS, SheetLog

from tests.test_sheets import MemorySheet


class FakeMarket:
    def constituents(self, symbol):
        assert symbol == "HSI.HK"
        return [
            {"symbol": "5.HK", "name": "HSBC", "last_done": "50"},
            {"symbol": "700.HK", "name": "Tencent", "last_done": "400"},
            {"symbol": "388.HK", "name": "HKEX", "last_done": "10"},
        ]

    def static_refs(self, symbols):
        return {
            "700.HK": {"lot_size": 100, "currency": "HKD", "name": "Tencent"},
            "5.HK": {"lot_size": 400, "currency": "HKD", "name": "HSBC"},
            "388.HK": {"lot_size": 100, "currency": "HKD", "name": "HKEX"},
        }

    def load(self, symbol, datasets, currency):
        bars = [
            {"close": 400 if symbol == "700.HK" else 50, "high": 410, "low": 390, "volume": 1000, "timestamp": index}
            for index in range(40)
        ]
        if symbol == "388.HK":
            bars = [
                {"close": 10, "high": 11, "low": 9, "volume": 100, "timestamp": index}
                for index in range(40)
            ]
        return {
            "kline_1y": bars,
            "institution_rating": {"latest": {"buy": 3, "hold": 1, "sell": 0}},
        }, []


class FakeJev:
    def __init__(self):
        self.symbols = []

    def decide(self, state, question):
        self.symbols.append(state["symbol"])
        choice = {"700.HK": "buy", "5.HK": "hold", "388.HK": "sell"}[state["symbol"]]
        return {
            "choice": choice,
            "probabilities": {"buy": 0.5, "hold": 0.4, "sell": 0.1},
            "confidence": 0.42,
            "model": "jev-1.13.0",
            "input_tokens": 80,
        }


def _config(**overrides):
    data = {
        "universe": {"index_symbol": "HSI.HK"},
        "datasets": {
            "kline": {"enabled": True, "lookback_days": 365},
            "institution_rating": {"enabled": True},
        },
        "portfolio": {"amount": 100000, "currency": "HKD"},
        "sheets": {"enabled": False},
        "typesafe": {"max_tokens": 32000, "price_per_million_tokens": 0.042},
    }
    data.update(overrides)
    return config_from_dict(data)


def test_collect_sorts_symbols_and_compacts_without_calling_jev():
    bundles = collect(_config(), FakeMarket())
    assert [bundle.symbol for bundle in bundles] == ["388.HK", "5.HK", "700.HK"]
    assert bundles[-1].price == 400
    assert bundles[-1].lot_size == 100
    assert "kline_1y" in bundles[-1].state
    assert "last_close" in bundles[-1].state["kline_1y"]
    report = estimate(_config(), bundles)
    assert report.over_limit == []
    assert report.price_per_million == 0.042


def test_run_sizes_paper_buys_and_upserts_instead_of_duplicating(tmp_path):
    config = _config()
    bundles = collect(config, FakeMarket())
    positions = SheetLog(
        MemorySheet(
            [
                POSITION_HEADERS,
                ["5.HK", "2024-05-01T00:00:00Z", "2024-05-01T00:00:00Z", "buy", "400", "40", "16000"],
            ]
        ),
        POSITION_HEADERS,
    )
    probabilities = SheetLog(MemorySheet(), ["ticker", "created_at", "updated_at", "choice", "confidence", "probabilities", "model", "input_tokens"])
    jev = FakeJev()
    rows = execute(config, bundles, jev, positions, probabilities)
    by_symbol = {row.symbol: row for row in rows}
    assert by_symbol["700.HK"].status == "buy"
    assert by_symbol["700.HK"].shares == 200  # 100000 budget, lot 100, price 400
    assert by_symbol["700.HK"].total_price == 80000
    assert by_symbol["5.HK"].status == "hold"
    assert by_symbol["5.HK"].shares == 400
    assert by_symbol["388.HK"].status == "sell"
    assert by_symbol["388.HK"].shares == 0
    tickers = [row[0] for row in positions.rows[1:]]
    assert tickers.count("5.HK") == 1
    held = [row for row in positions.rows[1:] if row[0] == "5.HK"][0]
    assert held[1] == "2024-05-01T00:00:00Z"
    assert held[3] == "hold"
    assert probabilities.rows[0][0] == "ticker"
    assert {row[0] for row in probabilities.rows[1:]} == {"388.HK", "5.HK", "700.HK"}
    assert "0.5" in probabilities.rows[1][5] or "0.500" in str(probabilities.rows[1][5])


def test_over_limit_ticker_is_flagged_and_not_sent_to_jev():
    config = _config(typesafe={"max_tokens": 32, "price_per_million_tokens": 0.042})
    bundles = collect(config, FakeMarket())
    jev = FakeJev()
    rows = execute(config, bundles, jev, None, None)
    assert jev.symbols == []
    assert {row.status for row in rows} == {"skipped"}
    assert all("limit" in row.reason for row in rows)


def test_states_dir_estimate_reads_files_only(tmp_path):
    (tmp_path / "700.HK.json").write_text(
        '{"symbol": "700.HK", "kline_1y": {"last_close": 400, "bars": 10}}',
        encoding="utf-8",
    )
    bundles = load_state_dir(tmp_path)
    report = estimate(_config(), bundles)
    assert report.rows[0].symbol == "700.HK"
    assert report.rows[0].total_tokens > report.question_tokens
    assert bundles[0].price == 400
