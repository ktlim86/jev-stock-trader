"""Load config.yaml and the process environment.

Longbridge's own client also reads ``.env`` when it builds a config. This
loader fills any variables that are still unset so TypeSafe and Google
credentials in the same file are visible before that happens.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DATASET_ORDER = (
    "financial_report",
    "institution_rating",
    "forecast_eps",
    "valuation_comparison",
    "shareholder",
    "insider_trades",
    "kline",
)

_DATASET_ALIASES = {"compare": "valuation_comparison"}


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Set variables from a ``.env`` file without overriding the real environment."""
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


@dataclass
class DatasetConfig:
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class UniverseConfig:
    index_symbol: str = "HSI.HK"
    max_tickers: int | None = None


@dataclass
class CompactConfig:
    financial_periods: int | None = 5
    shareholder_periods: int = 2
    forecast_items: int = 6
    valuation_history_points: int | None = 12
    weekly_closes: int = 26


@dataclass
class QuestionConfig:
    instructions: str = (
        "Given `state` for this stock, for a 3-4 month swing trade with max "
        "30% drawdown tolerance, should we buy, hold or sell?"
    )
    buy: str = (
        "Fundamentals, analyst view and trend favour upside over 3-4 months"
    )
    hold: str = "Mixed or neutral evidence"
    sell: str = "Deteriorating fundamentals/trend or downside risk dominates"


@dataclass
class TypeSafeConfig:
    model: str = "jev-latest"
    endpoint: str = "https://api.typesafe.ai/v1/systemone"
    api_key_env: str = "TYPESAFE_API_KEY"
    max_tokens: int = 32000
    price_per_million_tokens: float = 0.042
    max_retries: int = 5
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 32.0
    timeout_seconds: float = 120.0
    question: QuestionConfig = field(default_factory=QuestionConfig)


@dataclass
class PortfolioConfig:
    amount: float = 1_000_000
    currency: str = "HKD"
    default_lot_size_hk: int = 100
    default_lot_size: int = 1


@dataclass
class MarketConfig:
    pause_seconds: float = 0.0


@dataclass
class SheetsConfig:
    enabled: bool = True
    spreadsheet_id_env: str = "GOOGLE_SHEET_ID"
    credentials_env: str = "GOOGLE_SERVICE_ACCOUNT_JSON"
    spreadsheet_id: str = ""
    credentials: str = ""
    positions_tab: str = "positions"
    probabilities_tab: str = "jev_probabilities"


@dataclass
class AppConfig:
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    datasets: dict[str, DatasetConfig] = field(default_factory=dict)
    compact: CompactConfig = field(default_factory=CompactConfig)
    typesafe: TypeSafeConfig = field(default_factory=TypeSafeConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    sheets: SheetsConfig = field(default_factory=SheetsConfig)

    def enabled_datasets(self) -> list[tuple[str, DatasetConfig]]:
        return [
            (name, self.datasets[name])
            for name in DATASET_ORDER
            if name in self.datasets and self.datasets[name].enabled
        ]


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _parse_dataset(raw: Any) -> DatasetConfig:
    if not isinstance(raw, dict):
        return DatasetConfig(enabled=bool(raw))
    options = dict(raw.get("args") or {})
    for key, value in raw.items():
        if key in {"enabled", "tool", "args"}:
            continue
        options[key] = value
    return DatasetConfig(enabled=bool(raw.get("enabled", True)), options=options)


def _question(raw: Any) -> QuestionConfig:
    base = QuestionConfig()
    data = _as_dict(raw)
    criteria = _as_dict(data.get("criteria"))
    return QuestionConfig(
        instructions=str(data.get("instructions", base.instructions)),
        buy=str(criteria.get("buy", data.get("buy", base.buy))),
        hold=str(criteria.get("hold", data.get("hold", base.hold))),
        sell=str(criteria.get("sell", data.get("sell", base.sell))),
    )


def _nullable_int(raw: dict[str, Any], key: str, default: int | None) -> int | None:
    if key not in raw:
        return default
    value = raw[key]
    if value is None:
        return None
    return int(value)


def config_from_dict(data: dict[str, Any]) -> AppConfig:
    universe_raw = _as_dict(data.get("universe"))
    max_tickers = universe_raw.get("max_tickers", None)
    universe = UniverseConfig(
        index_symbol=str(universe_raw.get("index_symbol", "HSI.HK")),
        max_tickers=None if max_tickers in (None, "null") else int(max_tickers),
    )

    datasets: dict[str, DatasetConfig] = {
        name: DatasetConfig(enabled=False) for name in DATASET_ORDER
    }
    for key, raw in _as_dict(data.get("datasets")).items():
        name = _DATASET_ALIASES.get(str(key), str(key))
        if name not in datasets and name not in DATASET_ORDER:
            continue
        datasets[name] = _parse_dataset(raw)

    compact_raw = _as_dict(data.get("compact"))
    compact = CompactConfig(
        financial_periods=_nullable_int(compact_raw, "financial_periods", 5),
        shareholder_periods=int(compact_raw.get("shareholder_periods", 2)),
        forecast_items=int(compact_raw.get("forecast_items", 6)),
        valuation_history_points=_nullable_int(compact_raw, "valuation_history_points", 12),
        weekly_closes=int(compact_raw.get("weekly_closes", 26)),
    )

    ts_raw = _as_dict(data.get("typesafe"))
    typesafe = TypeSafeConfig(
        model=str(ts_raw.get("model", "jev-latest")),
        endpoint=str(ts_raw.get("endpoint", "https://api.typesafe.ai/v1/systemone")),
        api_key_env=str(ts_raw.get("api_key_env", "TYPESAFE_API_KEY")),
        max_tokens=int(ts_raw.get("max_tokens", 32000)),
        price_per_million_tokens=float(ts_raw.get("price_per_million_tokens", 0.042)),
        max_retries=int(ts_raw.get("max_retries", 5)),
        backoff_initial_seconds=float(ts_raw.get("backoff_initial_seconds", 1.0)),
        backoff_max_seconds=float(ts_raw.get("backoff_max_seconds", 32.0)),
        timeout_seconds=float(ts_raw.get("timeout_seconds", 120.0)),
        question=_question(ts_raw.get("question")),
    )

    market_raw = _as_dict(data.get("market"))
    market = MarketConfig(pause_seconds=float(market_raw.get("pause_seconds", 0.0)))

    port_raw = _as_dict(data.get("portfolio"))
    portfolio = PortfolioConfig(
        amount=float(port_raw.get("amount", 1_000_000)),
        currency=str(port_raw.get("currency", "HKD")),
        default_lot_size_hk=int(port_raw.get("default_lot_size_hk", 100)),
        default_lot_size=int(port_raw.get("default_lot_size", 1)),
    )

    sheets_raw = _as_dict(data.get("sheets"))
    probabilities = sheets_raw.get("probabilities_tab", "jev_probabilities")
    sheets = SheetsConfig(
        enabled=bool(sheets_raw.get("enabled", True)),
        spreadsheet_id_env=str(sheets_raw.get("spreadsheet_id_env", "GOOGLE_SHEET_ID")),
        credentials_env=str(
            sheets_raw.get("credentials_env", "GOOGLE_SERVICE_ACCOUNT_JSON")
        ),
        spreadsheet_id=str(sheets_raw.get("spreadsheet_id") or ""),
        credentials=str(sheets_raw.get("credentials") or ""),
        positions_tab=str(sheets_raw.get("positions_tab", "positions")),
        probabilities_tab="" if probabilities in (None, "") else str(probabilities),
    )
    cfg = AppConfig(
        universe=universe,
        datasets=datasets,
        compact=compact,
        typesafe=typesafe,
        portfolio=portfolio,
        market=market,
        sheets=sheets,
    )
    _validate(cfg)
    return cfg


def _validate(cfg: AppConfig) -> None:
    if not cfg.universe.index_symbol.strip():
        raise ValueError("universe.index_symbol is required")
    if cfg.universe.max_tickers is not None and cfg.universe.max_tickers < 1:
        raise ValueError("universe.max_tickers must be null or a positive integer")
    if cfg.portfolio.amount <= 0:
        raise ValueError("portfolio.amount must be positive")
    if cfg.typesafe.max_tokens < 1:
        raise ValueError("typesafe.max_tokens must be positive")
    if cfg.typesafe.price_per_million_tokens < 0:
        raise ValueError("typesafe.price_per_million_tokens must be >= 0")
    if cfg.typesafe.max_retries < 0:
        raise ValueError("typesafe.max_retries must be >= 0")
    if not cfg.enabled_datasets():
        raise ValueError("enable at least one dataset")
    if cfg.market.pause_seconds < 0:
        raise ValueError("market.pause_seconds must be >= 0")
    if not cfg.typesafe.question.instructions.strip():
        raise ValueError("typesafe.question.instructions is required")
    for label in (cfg.typesafe.question.buy, cfg.typesafe.question.hold, cfg.typesafe.question.sell):
        if not label.strip():
            raise ValueError("buy, hold, and sell criteria must be non-empty")


def load_config(path: str | os.PathLike[str]) -> AppConfig:
    file = Path(path)
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{file} must contain a YAML mapping")
    return config_from_dict(data)


def decision_question(cfg: AppConfig) -> dict[str, Any]:
    """Choice question sent to Jev. The key ``decision`` is not part of the prompt."""
    question = cfg.typesafe.question
    return {
        "decision": {
            "type": "choice",
            "instructions": question.instructions,
            "criteria": {
                "buy": question.buy,
                "hold": question.hold,
                "sell": question.sell,
            },
        }
    }


def env_value(name: str, fallback: str = "") -> str:
    return os.environ.get(name, "").strip() or fallback
