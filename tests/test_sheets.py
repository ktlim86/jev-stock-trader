import json

import pytest

from jev_trader.sheets import (
    POSITION_HEADERS,
    PROBABILITY_HEADERS,
    SheetLog,
    SheetSchemaError,
    existing_share_amounts,
    plan_upsert,
    position_record,
    probability_record,
)


class MemorySheet:
    def __init__(self, rows=None):
        self.rows = [list(row) for row in (rows or [])]
        self.updates = []
        self.appends = []

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def update(self, values, range_name=None):
        self.updates.append((values, range_name))
        row_number = int("".join(ch for ch in range_name.split(":")[0] if ch.isdigit()))
        self.rows[row_number - 1] = list(values[0])

    def append_row(self, values):
        self.appends.append(list(values))
        self.rows.append(list(values))


def test_headers_match_the_required_positions_columns():
    assert POSITION_HEADERS == [
        "ticker",
        "created_at",
        "updated_at",
        "status",
        "share amount",
        "price",
        "total price",
    ]


def test_upsert_updates_the_existing_ticker_and_keeps_created_at():
    sheet = MemorySheet(
        [
            POSITION_HEADERS,
            ["700.HK", "2024-01-02T00:00:00Z", "2024-01-02T00:00:00Z", "buy", "100", "10", "1000", "note"],
        ]
    )
    log = SheetLog(sheet, POSITION_HEADERS)
    log.upsert(
        position_record(
            ticker="700.HK",
            created_at="2026-10-12T00:00:00Z",
            updated_at="2026-10-12T00:00:00Z",
            status="hold",
            shares=100,
            price=12.5,
            total_price=1250,
        )
    )
    assert sheet.updates[0][1] == "A2:G2"
    assert len(sheet.rows) == 2
    row = sheet.rows[1]
    assert row[1] == "2024-01-02T00:00:00Z"
    assert row[2] == "2026-10-12T00:00:00Z"
    assert row[3] == "hold"
    assert row[4] == 100
    assert existing_share_amounts(log)["700.HK"] == 100


def test_new_ticker_is_appended_and_a_bad_header_is_rejected():
    sheet = MemorySheet()
    log = SheetLog(sheet, POSITION_HEADERS)
    log.upsert(
        position_record(
            ticker="5.HK",
            created_at="t0",
            updated_at="t0",
            status="sell",
            shares=0,
            price=20,
            total_price=0,
        )
    )
    assert sheet.rows[0] == POSITION_HEADERS
    assert sheet.rows[1][0] == "5.HK"
    assert sheet.rows[1][3] == "sell"
    with pytest.raises(SheetSchemaError):
        plan_upsert([["symbol", "when"]], POSITION_HEADERS, {"ticker": "1.HK"})


def test_probability_record_keeps_the_raw_map():
    record = probability_record(
        ticker="700.HK",
        created_at="t",
        updated_at="t",
        choice="buy",
        confidence=0.5,
        probabilities={"sell": 0.1, "buy": 0.7, "hold": 0.2},
        model="jev-1.13.0",
        input_tokens=90,
    )
    assert list(record) == PROBABILITY_HEADERS
    assert json.loads(record["probabilities"]) == {"buy": 0.7, "hold": 0.2, "sell": 0.1}
