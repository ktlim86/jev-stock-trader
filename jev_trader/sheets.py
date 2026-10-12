"""Upsert paper positions into Google Sheets.

The positions tab uses these columns, in this order:

    ticker, created_at, updated_at, status, share amount, price, total price

A ticker that is already on the sheet is updated in place. ``created_at``
stays as it was; ``updated_at`` changes. The optional probabilities tab stores
the Jev choice, confidence, and probability map for the same ticker.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

POSITION_HEADERS = [
    "ticker",
    "created_at",
    "updated_at",
    "status",
    "share amount",
    "price",
    "total price",
]

PROBABILITY_HEADERS = [
    "ticker",
    "created_at",
    "updated_at",
    "choice",
    "confidence",
    "probabilities",
    "model",
    "input_tokens",
]


class SheetSchemaError(RuntimeError):
    pass


class Worksheet(Protocol):
    def get_all_values(self) -> list[list[Any]]: ...

    def update(self, values: list[list[Any]], range_name: str | None = None) -> Any: ...

    def append_row(self, values: list[Any]) -> Any: ...


def plan_upsert(
    rows: list[list[Any]],
    header: list[str],
    record: dict[str, Any],
) -> list[tuple[Any, ...]]:
    """Return ``("append", values)`` and ``("update", row_number, values)`` operations.

    Row numbers are 1-based, matching a worksheet. An empty sheet receives the
    header first. An existing ticker keeps its ``created_at``.
    """
    width = len(header)
    if not rows or not any(cell not in ("", None) for cell in rows[0]):
        ops: list[tuple[Any, ...]] = [("append", list(header))]
        ops.append(("append", [_cell(record, header, key) for key in header]))
        return ops
    current = _header_cells(rows[0], width)
    if current != header:
        raise SheetSchemaError(
            f"worksheet header {current!r} does not match {header!r}. "
            "The first columns must be exactly those names; extra columns to the right are left alone."
        )
    ticker = str(record["ticker"])
    values = [_cell(record, header, key) for key in header]
    created_idx = header.index("created_at")
    for offset, row in enumerate(rows[1:], start=2):
        if row and str(row[0]).strip() == ticker:
            if len(row) > created_idx and str(row[created_idx]).strip():
                values[created_idx] = row[created_idx]
            return [("update", offset, values)]
    return [("append", values)]


def apply_ops(worksheet: Worksheet, ops: list[tuple[Any, ...]], width: int) -> None:
    end_col = _column_letter(width)
    for op in ops:
        if op[0] == "append":
            worksheet.append_row(list(op[1]))
        elif op[0] == "update":
            row_number = int(op[1])
            worksheet.update([list(op[2])], range_name=f"A{row_number}:{end_col}{row_number}")
        else:
            raise ValueError(f"unknown sheet operation {op[0]}")


def position_record(
    *,
    ticker: str,
    created_at: str,
    updated_at: str,
    status: str,
    shares: int,
    price: float | None,
    total_price: float,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "created_at": created_at,
        "updated_at": updated_at,
        "status": status,
        "share amount": shares,
        "price": "" if price is None else price,
        "total price": total_price,
    }


def probability_record(
    *,
    ticker: str,
    created_at: str,
    updated_at: str,
    choice: str,
    confidence: float,
    probabilities: dict[str, float],
    model: str | None,
    input_tokens: Any,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "created_at": created_at,
        "updated_at": updated_at,
        "choice": choice,
        "confidence": confidence,
        "probabilities": json.dumps(probabilities, separators=(",", ":"), sort_keys=True),
        "model": model or "",
        "input_tokens": "" if input_tokens is None else input_tokens,
    }


class SheetLog:
    """Read existing rows, upsert one record, and keep a local copy so later reads see the write."""

    def __init__(self, worksheet: Worksheet, header: list[str]) -> None:
        self.worksheet = worksheet
        self.header = header
        self.rows = [list(row) for row in worksheet.get_all_values()]

    def upsert(self, record: dict[str, Any]) -> None:
        ops = plan_upsert(self.rows, self.header, record)
        apply_ops(self.worksheet, ops, len(self.header))
        self.rows = _apply_local(self.rows, self.header, ops)

    def column(self, ticker: str, name: str) -> str:
        if not self.rows:
            return ""
        try:
            index = self.header.index(name)
        except ValueError:
            return ""
        for row in self.rows[1:]:
            if row and str(row[0]).strip() == ticker and len(row) > index:
                return str(row[index]).strip()
        return ""


def existing_share_amounts(log: SheetLog | None) -> dict[str, int]:
    if log is None or not log.rows:
        return {}
    try:
        index = log.header.index("share amount")
    except ValueError:
        return {}
    found: dict[str, int] = {}
    for row in log.rows[1:]:
        if not row or not str(row[0]).strip():
            continue
        raw = row[index] if len(row) > index else ""
        found[str(row[0]).strip()] = _as_int(raw)
    return found


def open_sheet_logs(
    *,
    spreadsheet_id: str,
    credentials: str,
    positions_tab: str,
    probabilities_tab: str,
) -> tuple[SheetLog, SheetLog | None]:
    import json as _json

    import gspread
    from google.oauth2.service_account import Credentials

    info_or_path = credentials.strip()
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if info_or_path.startswith("{"):
        creds = Credentials.from_service_account_info(_json.loads(info_or_path), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(info_or_path, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)
    positions = SheetLog(_ensure_worksheet(spreadsheet, positions_tab, POSITION_HEADERS), POSITION_HEADERS)
    probabilities = None
    if probabilities_tab:
        probabilities = SheetLog(
            _ensure_worksheet(spreadsheet, probabilities_tab, PROBABILITY_HEADERS),
            PROBABILITY_HEADERS,
        )
    return positions, probabilities


def _ensure_worksheet(spreadsheet: Any, title: str, header: list[str]) -> Any:
    import gspread

    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=max(len(header), 8))
    return worksheet


def _header_cells(row: list[Any], width: int) -> list[str]:
    cells = [str(cell).strip() if cell is not None else "" for cell in row]
    if len(cells) < width:
        return cells
    return cells[:width]


def _cell(record: dict[str, Any], header: list[str], key: str) -> Any:
    if key not in record:
        raise KeyError(key)
    return record[key]


def _column_letter(width: int) -> str:
    letters = ""
    n = width
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _apply_local(
    rows: list[list[Any]], header: list[str], ops: list[tuple[Any, ...]]
) -> list[list[Any]]:
    updated = [list(row) for row in rows]
    for op in ops:
        if op[0] == "append":
            updated.append(list(op[1]))
        elif op[0] == "update":
            index = int(op[1]) - 1
            while len(updated) <= index:
                updated.append([])
            updated[index] = list(op[2])
    if updated and updated[0] != header and updated[0] == list(header):
        updated[0] = list(header)
    return updated


def _as_int(value: Any) -> int:
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0
