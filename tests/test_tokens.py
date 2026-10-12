from jev_trader.config import AppConfig, config_from_dict, decision_question
from jev_trader.tokens import format_report, report_tokens


def test_report_flags_the_limit_and_prices_input_tokens():
    question = {"decision": {"type": "choice", "instructions": "buy hold or sell?", "criteria": {"buy": "b", "hold": "h", "sell": "s"}}}
    states = [("700.HK", {"symbol": "700.HK", "note": "ok"}), ("5.HK", {"symbol": "5.HK", "blob": "word " * 5000})]
    report = report_tokens(states, question, limit=500, price_per_million=0.042)
    assert report.rows[0].question_tokens == report.question_tokens
    assert report.rows[0].total_tokens == report.rows[0].state_tokens + report.question_tokens
    assert not report.rows[0].over_limit(report.rows[0].total_tokens)
    assert report.rows[0].over_limit(report.rows[0].total_tokens - 1)
    assert [row.symbol for row in report.over_limit] == ["5.HK"]
    assert report.estimated_cost_usd == report.total_tokens / 1_000_000 * 0.042
    text = format_report(report)
    assert "OVER LIMIT" in text
    assert "5.HK" in text
    assert "estimated_cost_usd:" in text


def test_default_price_is_four_point_two_cents_per_million():
    cfg = config_from_dict({"datasets": {"kline": {"enabled": True}}})
    assert isinstance(cfg, AppConfig)
    assert cfg.typesafe.price_per_million_tokens == 0.042
    question = decision_question(cfg)
    assert question["decision"]["type"] == "choice"
    assert set(question["decision"]["criteria"]) == {"buy", "hold", "sell"}
