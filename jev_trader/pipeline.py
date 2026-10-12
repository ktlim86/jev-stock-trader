"""Build compact states, estimate tokens, ask Jev, and log paper positions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jev_trader.compact import build_state
from jev_trader.config import AppConfig, decision_question
from jev_trader.jev import JevError
from jev_trader.portfolio import size_positions
from jev_trader.sheets import (
    POSITION_HEADERS,
    PROBABILITY_HEADERS,
    SheetLog,
    existing_share_amounts,
    position_record,
    probability_record,
)
from jev_trader.tokens import TokenReport, report_tokens

logger = logging.getLogger(__name__)

_META = {"symbol", "name", "currency"}


@dataclass
class Bundle:
    symbol: str
    name: str
    price: float | None
    lot_size: int | None
    state: dict[str, Any]
    errors: list[str] = field(default_factory=list)


@dataclass
class RunRow:
    symbol: str
    status: str
    reason: str = ""
    shares: int = 0
    price: float | None = None
    total_price: float = 0.0
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    note: str = ""


def collect(config: AppConfig, market: Any) -> list[Bundle]:
    members = [
        row
        for row in market.constituents(config.universe.index_symbol)
        if row.get("symbol")
    ]
    members.sort(key=lambda row: row["symbol"])
    if config.universe.max_tickers is not None:
        members = members[: config.universe.max_tickers]
    symbols = [row["symbol"] for row in members]
    logger.info(
        "%s: using %s constituents", config.universe.index_symbol, len(symbols)
    )
    refs = market.static_refs(symbols) if symbols else {}
    datasets = config.enabled_datasets()
    bundles: list[Bundle] = []
    for member in members:
        symbol = member["symbol"]
        ref = refs.get(symbol) or {}
        raw, errors = market.load(symbol, datasets, config.portfolio.currency)
        name = ref.get("name") or member.get("name") or ""
        currency = ref.get("currency") or member.get("currency") or config.portfolio.currency
        state = build_state(
            raw,
            config.compact,
            symbol=symbol,
            name=str(name) if name else None,
            currency=str(currency) if currency else None,
        )
        lot_size = _positive_int(ref.get("lot_size"))
        bundles.append(
            Bundle(
                symbol=symbol,
                name=str(name),
                price=_price(raw.get("kline_1y"), member.get("last_done")),
                lot_size=lot_size,
                state=state,
                errors=errors,
            )
        )
    return bundles


def load_state_dir(path: str | Path) -> list[Bundle]:
    """Load compact states from ``SYMBOL.json`` files. No network."""
    root = Path(path)
    bundles: list[Bundle] = []
    for file in sorted(root.glob("*.json")):
        state = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError(f"{file} must be a JSON object")
        symbol = str(state.get("symbol") or file.stem)
        state = dict(state)
        state["symbol"] = symbol
        kline = state.get("kline_1y")
        price = None
        if isinstance(kline, dict) and kline.get("last_close") not in (None, ""):
            price = float(kline["last_close"])
        bundles.append(
            Bundle(
                symbol=symbol,
                name=str(state.get("name") or ""),
                price=price,
                lot_size=None,
                state=state,
            )
        )
    return bundles


def estimate(config: AppConfig, bundles: list[Bundle]) -> TokenReport:
    return report_tokens(
        [(bundle.symbol, bundle.state) for bundle in bundles],
        decision_question(config),
        limit=config.typesafe.max_tokens,
        price_per_million=config.typesafe.price_per_million_tokens,
    )


def execute(
    config: AppConfig,
    bundles: list[Bundle],
    jev: Any,
    positions: SheetLog | None,
    probabilities: SheetLog | None,
    *,
    now: datetime | None = None,
) -> list[RunRow]:
    """Ask Jev for each ticker that fits the token limit, then upsert the sheet.

    Over-limit tickers are flagged and skipped. No TypeSafe call is made for them.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    question = decision_question(config)
    token_rows = {
        row.symbol: row
        for row in estimate(config, bundles).rows
    }
    decided: list[dict[str, Any]] = []
    answers: dict[str, dict[str, Any]] = {}
    results: list[RunRow] = []
    for bundle in bundles:
        tokens = token_rows[bundle.symbol]
        if tokens.over_limit(config.typesafe.max_tokens):
            results.append(
                RunRow(
                    symbol=bundle.symbol,
                    status="skipped",
                    reason=(
                        f"state+question is {tokens.total_tokens} tokens, "
                        f"limit is {config.typesafe.max_tokens}"
                    ),
                )
            )
            continue
        if not any(key not in _META for key in bundle.state):
            reason = "; ".join(bundle.errors) or "no dataset was returned"
            results.append(RunRow(symbol=bundle.symbol, status="skipped", reason=reason))
            continue
        try:
            answer = jev.decide(bundle.state, question)
        except JevError as exc:
            results.append(RunRow(symbol=bundle.symbol, status="error", reason=str(exc)))
            continue
        answers[bundle.symbol] = answer
        decided.append({"symbol": bundle.symbol, "choice": answer["choice"]})

    sized = size_positions(
        decided,
        portfolio_amount=config.portfolio.amount,
        prices={bundle.symbol: bundle.price for bundle in bundles},
        lot_sizes={bundle.symbol: bundle.lot_size for bundle in bundles},
        existing_shares=existing_share_amounts(positions),
        default_lot_size_hk=config.portfolio.default_lot_size_hk,
        default_lot_size=config.portfolio.default_lot_size,
    )
    for position in sized:
        answer = answers[position.symbol]
        if positions is not None:
            positions.upsert(
                position_record(
                    ticker=position.symbol,
                    created_at=stamp,
                    updated_at=stamp,
                    status=position.status,
                    shares=position.shares,
                    price=position.price,
                    total_price=position.total_price,
                )
            )
        if probabilities is not None:
            probabilities.upsert(
                probability_record(
                    ticker=position.symbol,
                    created_at=stamp,
                    updated_at=stamp,
                    choice=answer["choice"],
                    confidence=answer["confidence"],
                    probabilities=answer["probabilities"],
                    model=answer.get("model"),
                    input_tokens=answer.get("input_tokens"),
                )
            )
        results.append(
            RunRow(
                symbol=position.symbol,
                status=position.status,
                shares=position.shares,
                price=position.price,
                total_price=position.total_price,
                confidence=answer["confidence"],
                probabilities=answer["probabilities"],
                note=position.note,
            )
        )
    results.sort(key=lambda row: row.symbol)
    return results


def format_run(rows: list[RunRow]) -> str:
    lines = []
    for row in rows:
        if row.status in {"skipped", "error"}:
            lines.append(f"{row.symbol}: {row.status}: {row.reason}")
            continue
        price = "" if row.price is None else f"{row.price:.4f}"
        note = f" ({row.note})" if row.note else ""
        probs = ""
        if row.probabilities:
            probs = " " + " ".join(
                f"p_{name}={value:.3f}" for name, value in sorted(row.probabilities.items())
            )
        confidence = "" if row.confidence is None else f" confidence={row.confidence:.3f}"
        lines.append(
            f"{row.symbol}: {row.status} shares={row.shares} price={price} "
            f"total={row.total_price:.2f}{confidence}{probs}{note}"
        )
    return "\n".join(lines)


def _price(kline: Any, last_done: Any) -> float | None:
    if isinstance(kline, list) and kline:
        rows = [bar for bar in kline if isinstance(bar, dict)]
        rows.sort(key=lambda bar: _stamp(bar.get("timestamp")))
        for bar in reversed(rows):
            parsed = _float(bar.get("close"))
            if parsed is not None and parsed > 0:
                return parsed
    if isinstance(kline, dict):
        parsed = _float(kline.get("last_close"))
        if parsed is not None and parsed > 0:
            return parsed
    parsed = _float(last_done)
    if parsed is not None and parsed > 0:
        return parsed
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _float(value: Any) -> float | None:
    if value is None or value is True or value is False or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0
