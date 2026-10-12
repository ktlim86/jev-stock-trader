"""``python -m jev_trader estimate|run``.

``estimate`` builds compact states and prints token counts and cost. It never
calls TypeSafe and never writes the sheet. ``--states-dir`` skips Longbridge
as well and counts JSON files already on disk.

``run`` is the daily paper pass: read-only market data, one Jev choice per
ticker, then an upsert into Google Sheets. It never submits an order.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from jev_trader.config import env_value, load_config, load_dotenv
from jev_trader.market import connect
from jev_trader.pipeline import collect, estimate, execute, format_run, load_state_dir
from jev_trader.sheets import open_sheet_logs
from jev_trader.tokens import format_report


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser = argparse.ArgumentParser(prog="jev_trader")
    sub = parser.add_subparsers(dest="command", required=True)

    estimate_parser = sub.add_parser(
        "estimate",
        parents=[common],
        help="Count tokens and cost. Does not call Jev.",
    )
    estimate_parser.add_argument(
        "--states-dir",
        help="Read SYMBOL.json states from this directory instead of calling Longbridge",
    )
    _add_common(estimate_parser)

    run_parser = sub.add_parser(
        "run",
        parents=[common],
        help="Paper run: Jev decisions and sheet upsert. No orders.",
    )
    _add_common(run_parser)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        return _dispatch(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    config = load_config(args.config)
    if args.portfolio_amount is not None:
        config.portfolio.amount = float(args.portfolio_amount)
        if config.portfolio.amount <= 0:
            print("portfolio amount must be positive", file=sys.stderr)
            return 1
    if args.max_tickers is not None:
        config.universe.max_tickers = int(args.max_tickers)
    if args.command == "estimate":
        return _estimate(config, args.states_dir)
    return _run(config)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--portfolio-amount",
        type=float,
        default=None,
        help="Override portfolio.amount for this process",
    )
    parser.add_argument(
        "--max-tickers",
        type=int,
        default=None,
        help="Override universe.max_tickers for this process",
    )


def _estimate(config, states_dir: str | None) -> int:
    if states_dir:
        bundles = load_state_dir(states_dir)
    else:
        bundles = collect(config, connect(config.market.pause_seconds))
    if not bundles:
        print("no tickers to estimate", file=sys.stderr)
        return 1
    print(format_report(estimate(config, bundles)))
    return 0


def _run(config) -> int:
    api_key = os.environ.get(config.typesafe.api_key_env, "").strip()
    if not api_key:
        print(
            f"Missing {config.typesafe.api_key_env}. estimate does not need it; run does.",
            file=sys.stderr,
        )
        return 1
    sheet_id = ""
    credentials = ""
    if config.sheets.enabled:
        sheet_id = env_value(config.sheets.spreadsheet_id_env, config.sheets.spreadsheet_id)
        credentials = env_value(config.sheets.credentials_env, config.sheets.credentials)
        if not sheet_id or not credentials:
            print(
                "sheets.enabled is true but the spreadsheet id or service-account "
                f"credentials are missing ({config.sheets.spreadsheet_id_env}, "
                f"{config.sheets.credentials_env}).",
                file=sys.stderr,
            )
            return 1
        if not credentials.strip().startswith("{") and not Path(credentials).is_file():
            print(f"service-account file not found: {credentials}", file=sys.stderr)
            return 1

    bundles = collect(config, connect(config.market.pause_seconds))
    if not bundles:
        print("no constituents returned", file=sys.stderr)
        return 1

    positions = None
    probabilities = None
    if config.sheets.enabled:
        positions, probabilities = open_sheet_logs(
            spreadsheet_id=sheet_id,
            credentials=credentials,
            positions_tab=config.sheets.positions_tab,
            probabilities_tab=config.sheets.probabilities_tab,
        )

    from jev_trader.jev import JevClient

    jev = JevClient(
        api_key=api_key,
        endpoint=config.typesafe.endpoint,
        model=config.typesafe.model,
        max_retries=config.typesafe.max_retries,
        backoff_initial_seconds=config.typesafe.backoff_initial_seconds,
        backoff_max_seconds=config.typesafe.backoff_max_seconds,
        timeout_seconds=config.typesafe.timeout_seconds,
    )
    rows = execute(config, bundles, jev, positions, probabilities)
    print(format_run(rows))
    if rows and all(row.status in {"error", "skipped"} for row in rows):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
