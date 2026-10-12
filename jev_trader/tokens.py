"""Count Jev input tokens with tiktoken cl100k_base.

Jev does not publish its tokenizer. cl100k_base is the same proxy the stage-1
estimator used. Counts are state JSON plus the choice-question JSON, which is
the 32k budget (state plus the longest question). Each ticker is its own
request, so the cost multiplies that sum by the configured price per million
input tokens. Output tokens are not billed in that price.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

_ENCODER = None


def encode_len(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    )
    return len(_encoder().encode(text))


def _encoder():
    global _ENCODER
    if _ENCODER is None:
        import tiktoken

        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return _ENCODER


@dataclass
class TickerTokens:
    symbol: str
    state_tokens: int
    question_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.state_tokens + self.question_tokens

    def over_limit(self, limit: int) -> bool:
        return self.total_tokens > limit


@dataclass
class TokenReport:
    rows: list[TickerTokens]
    question_tokens: int
    limit: int
    price_per_million: float

    @property
    def total_tokens(self) -> int:
        return sum(row.total_tokens for row in self.rows)

    @property
    def over_limit(self) -> list[TickerTokens]:
        return [row for row in self.rows if row.over_limit(self.limit)]

    @property
    def estimated_cost_usd(self) -> float:
        return self.total_tokens / 1_000_000 * self.price_per_million


def report_tokens(
    states: list[tuple[str, dict[str, Any]]],
    question: dict[str, Any],
    *,
    limit: int,
    price_per_million: float,
) -> TokenReport:
    question_tokens = encode_len(question)
    rows = [
        TickerTokens(symbol, encode_len(state), question_tokens)
        for symbol, state in states
    ]
    return TokenReport(
        rows=rows,
        question_tokens=question_tokens,
        limit=limit,
        price_per_million=price_per_million,
    )


def format_report(report: TokenReport) -> str:
    lines = [
        f"tickers: {len(report.rows)}",
        f"question_tokens: {report.question_tokens}",
        f"limit: {report.limit} (state + question)",
        f"price_per_million_usd: {report.price_per_million}",
        f"{'ticker':<16}{'state':>10}{'question':>10}{'total':>10}  status",
    ]
    for row in report.rows:
        status = "OVER LIMIT" if row.over_limit(report.limit) else "ok"
        lines.append(
            f"{row.symbol:<16}{row.state_tokens:>10}{row.question_tokens:>10}{row.total_tokens:>10}  {status}"
        )
    lines.append(
        f"{'TOTAL':<16}{'':>10}{'':>10}{report.total_tokens:>10}"
    )
    lines.append(f"estimated_cost_usd: {report.estimated_cost_usd:.6f}")
    lines.append(f"over_limit: {len(report.over_limit)}")
    if report.over_limit:
        names = ", ".join(row.symbol for row in report.over_limit)
        lines.append(f"over_limit_tickers: {names}")
    return "\n".join(lines)
