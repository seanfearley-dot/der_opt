from datetime import datetime
from pathlib import Path

import pytest

from der_opt.integrations.pge_nbt_export import (
    NBTExportPriceSeries,
    load_eec_table,
    parse_eec_price_sheet_pdf,
)
from der_opt.time_grid import TimeGrid

CSV_PATH = Path(__file__).parent.parent.parent / "examples" / "data" / "pge_nbt_eec_2026.csv"
PDF_PATH = Path(__file__).parent.parent.parent / "examples" / "data" / "pge_eec_price_sheet_2026.pdf"


def test_load_eec_table_has_expected_shape():
    table = load_eec_table(CSV_PATH)
    assert len(table) == 576  # 24 hours x 12 months x 2 day types
    assert set(table["day_type"].unique()) == {"weekday", "weekend_holiday"}


def test_values_matches_known_cells():
    series = NBTExportPriceSeries(CSV_PATH)
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1, start=datetime(2026, 1, 5))  # Monday
    assert series.values(tg)[0] == pytest.approx(0.09342 + 0.00089, abs=1e-5)


def test_weekday_vs_weekend_use_different_rates():
    series = NBTExportPriceSeries(CSV_PATH)
    weekday = NBTExportPriceSeries(CSV_PATH).values(
        TimeGrid(resolution_minutes=60, horizon_steps=1, start=datetime(2026, 1, 5))  # Monday
    )
    weekend = series.values(
        TimeGrid(resolution_minutes=60, horizon_steps=1, start=datetime(2026, 1, 10))  # Saturday
    )
    assert weekday[0] != pytest.approx(weekend[0])


def test_include_produced_false_omits_produced_component():
    with_produced = NBTExportPriceSeries(CSV_PATH, include_produced=True)
    without_produced = NBTExportPriceSeries(CSV_PATH, include_produced=False)
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1, start=datetime(2026, 1, 5))
    assert with_produced.values(tg)[0] > without_produced.values(tg)[0]


def test_requires_time_grid_start():
    series = NBTExportPriceSeries(CSV_PATH)
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1)
    with pytest.raises(ValueError):
        series.values(tg)


def test_parsed_extremes_match_price_sheets_own_legend():
    # PG&E's price sheet prints its own min/max legend bands; the full 288-cell
    # weekday table's actual extremes should match those printed bounds exactly.
    table = load_eec_table(CSV_PATH)
    weekday = table[table.day_type == "weekday"]
    assert weekday["produced_usd_per_kwh"].max() == pytest.approx(0.99821, abs=1e-5)
    assert weekday["delivered_usd_per_kwh"].max() == pytest.approx(0.35038, abs=1e-5)


def test_pdf_parser_reproduces_shipped_csv_exactly():
    pytest.importorskip("pdfplumber", reason="optional dependency (der-opt[pdf])")
    parsed = parse_eec_price_sheet_pdf(PDF_PATH)
    table = load_eec_table(CSV_PATH)

    assert sum(len(v) for v in parsed.values()) == 576
    for day_type, records in parsed.items():
        for rec in records:
            row = table[
                (table.day_type == day_type) & (table.month == rec["month"]) & (table.hour == rec["hour"])
            ]
            assert row["produced_usd_per_kwh"].iloc[0] == pytest.approx(rec["produced"], abs=1e-6)
            assert row["delivered_usd_per_kwh"].iloc[0] == pytest.approx(rec["delivered"], abs=1e-6)
