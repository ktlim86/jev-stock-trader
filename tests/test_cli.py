import inspect
import json

from jev_trader import cli
from jev_trader.cli import main
from jev_trader.config import config_from_dict


PROTOTYPE = """
universe:
  index_symbol: HSI.HK
  max_tickers: null
datasets:
  financial_report: {enabled: true, tool: financial_report, args: {kind: ALL, report_type: af}}
  institution_rating: {enabled: true, tool: institution_rating}
  forecast_eps: {enabled: true, tool: forecast_eps}
  compare: {enabled: true, tool: valuation_comparison, args: {currency: HKD}}
  shareholder: {enabled: false, tool: shareholder}
  insider_trades: {enabled: true, tool: shareholder_top}
  kline: {enabled: true, tool: history_candlesticks_by_date, lookback_days: 365, args: {period: day, forward_adjust: true, trade_sessions: intraday}}
typesafe:
  model: jev-latest
  endpoint: https://api.typesafe.ai/v1/systemone
  price_per_million_tokens: 0.042
  call_api: false
portfolio:
  amount: 250000
sheets: {enabled: false}
"""


def test_prototype_config_shape_is_accepted():
    import yaml

    cfg = config_from_dict(yaml.safe_load(PROTOTYPE))
    assert cfg.universe.index_symbol == "HSI.HK"
    assert cfg.universe.max_tickers is None
    assert cfg.datasets["shareholder"].enabled is False
    assert cfg.datasets["valuation_comparison"].enabled is True
    assert cfg.datasets["valuation_comparison"].options["currency"] == "HKD"
    assert cfg.datasets["financial_report"].options["kind"] == "ALL"
    assert cfg.datasets["financial_report"].options["report_type"] == "af"
    assert cfg.datasets["kline"].options["lookback_days"] == 365
    assert cfg.datasets["kline"].options["forward_adjust"] is True
    assert cfg.portfolio.amount == 250000
    assert cfg.typesafe.price_per_million_tokens == 0.042
    assert cfg.sheets.enabled is False


def test_shipped_config_loads():
    from jev_trader.config import load_config

    cfg = load_config("config.yaml")
    assert cfg.universe.index_symbol == "HSI.HK"
    assert cfg.sheets.positions_tab == "positions"
    assert cfg.typesafe.max_tokens == 32000


def test_estimate_command_does_not_call_jev_or_sheets(monkeypatch, tmp_path, capsys):
    state = {"symbol": "700.HK", "kline_1y": {"last_close": 400, "bars": 10, "rsi14": 48}}
    (tmp_path / "700.HK.json").write_text(json.dumps(state), encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "datasets:\n  kline: {enabled: true}\nsheets: {enabled: false}\nportfolio: {amount: 10}\n",
        encoding="utf-8",
    )

    def explode(*args, **kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(cli, "connect", explode)
    monkeypatch.setattr("jev_trader.jev.JevClient", explode)
    monkeypatch.setattr(cli, "open_sheet_logs", explode)
    code = main(["estimate", "--config", str(config), "--states-dir", str(tmp_path)])
    assert code == 0
    captured = capsys.readouterr()
    assert "700.HK" in captured.out
    assert "OVER LIMIT" not in captured.out
    assert "estimated_cost_usd:" in captured.out
    assert "Jev" not in inspect.getsource(cli._estimate)
    assert "open_sheet" not in inspect.getsource(cli._estimate)


class _Market:
    def constituents(self, symbol):
        return [{"symbol": "700.HK", "name": "Tencent", "last_done": "400"}]

    def static_refs(self, symbols):
        return {"700.HK": {"lot_size": 100, "currency": "HKD", "name": "Tencent"}}

    def load(self, symbol, datasets, currency):
        bars = [
            {"close": 400, "high": 410, "low": 390, "volume": 1000, "timestamp": index}
            for index in range(30)
        ]
        return {"kline_1y": bars, "forecast_eps": {"items": [{"forecast_eps_mean": "1"}]}}, []


class _Jev:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def decide(self, state, question):
        assert question["decision"]["type"] == "choice"
        assert kwargs_model(self) == "jev-latest"
        return {
            "choice": "buy",
            "probabilities": {"buy": 0.8, "hold": 0.1, "sell": 0.1},
            "confidence": 0.7,
            "model": "jev-1.13.0",
            "input_tokens": 50,
        }


def kwargs_model(client):
    return client.kwargs["model"]


def test_run_command_papers_a_buy(monkeypatch, tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "universe: {index_symbol: HSI.HK}",
                "datasets:",
                "  kline: {enabled: true}",
                "  forecast_eps: {enabled: true}",
                "portfolio: {amount: 100000}",
                "sheets: {enabled: false}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(cli, "connect", lambda pause_seconds=0: _Market())
    monkeypatch.setattr("jev_trader.jev.JevClient", _Jev)
    code = main(
        ["run", "--config", str(config), "--portfolio-amount", "80000", "--max-tickers", "1"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "700.HK: buy shares=200" in out
    assert "total=80000.00" in out
    assert "p_buy=0.800" in out


def test_run_refuses_to_start_without_a_typesafe_key(monkeypatch, tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(
        "datasets:\n  kline: {enabled: true}\nsheets: {enabled: false}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("should not fetch quotes")

    monkeypatch.setattr(cli, "connect", explode)
    code = main(["run", "--config", str(config)])
    assert code == 1
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err
