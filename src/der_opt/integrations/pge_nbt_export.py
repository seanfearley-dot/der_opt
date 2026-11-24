"""Adapter for PG&E's published Solar Billing Plan (California's Net Billing Tariff,
"NBT"/NEM 3.0) Energy Export Credit (EEC) values -- the real $/kWh a PG&E customer is
credited for exported solar energy, set by the CPUC's Avoided Cost Calculator and
varying by interconnection-application vintage year, month, weekday/weekend, and hour.
A customer's rate is locked to their application-year vintage for 9 years.

Source: PG&E's official EEC price sheets (https://www.pge.com/solarbilling ->
"PGE-EEC-Price-Sheets.zip", one PDF per vintage year). examples/data/export_rates/pge_nbt_eec_2026.csv
in this repo is the 2026 vintage, parsed from that PDF via parse_eec_price_sheet_pdf
below -- coordinate-based word extraction (pdfplumber's table-line heuristic merges
adjacent hour rows on this PDF's tight ~6pt row spacing, so extract_tables() is NOT
reliable here). Cross-validated against the sheet's own printed min/max legend: the
parsed Produced and Delivered extremes match the legend's stated bounds exactly.

Energy Produced credits only apply to customers who get bundled generation service
from PG&E; customers on a CCA (community choice aggregator) or Direct Access provider
should use that provider's own export pricing instead of the "produced" component here
(see PG&E's price sheet footnote) -- pass include_produced=False for that case.

Note on the data itself, not a parsing artifact: Energy Delivered spikes sharply (to
~$0.18-0.35/kWh) for a few hours in summer (June-September) around 5-9pm, reflecting
real capacity/resource-adequacy scarcity value baked into the avoided-cost
methodology during California's net-peak ramp -- a well-documented, if controversial,
feature of NBT pricing, not an extraction error.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from der_opt.pricing.sources import PriceSeries
from der_opt.time_grid import TimeGrid

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def load_eec_table(csv_path: str | Path) -> pd.DataFrame:
    """Reads an EEC CSV in the schema this module writes: columns day_type
    ("weekday"/"weekend_holiday"), month, hour, produced_usd_per_kwh,
    delivered_usd_per_kwh, total_usd_per_kwh."""
    return pd.read_csv(csv_path)


def eec_component_rates(csv_path: str | Path, timestamps) -> pd.DataFrame:
    """Per-interval generation ("produced") and delivery ("delivered") export credit
    rates, $/kWh. The two halves are kept separate because NBT applies them separately:
    generation credits only offset generation charges, delivery credits only delivery
    charges (NBT Special Condition 2.d). Weekends and PG&E holidays get the
    weekend/holiday rate (see der_opt.billing.holidays)."""
    from der_opt.billing.holidays import is_weekend_or_holiday

    table = load_eec_table(csv_path)
    lookup = {
        (row.day_type, row.month, int(row.hour)): (float(row.produced_usd_per_kwh), float(row.delivered_usd_per_kwh))
        for row in table.itertuples()
    }
    index = pd.DatetimeIndex(timestamps)
    generation, delivery = [], []
    for ts in index:
        day_type = "weekend_holiday" if is_weekend_or_holiday(ts.date()) else "weekday"
        key = (day_type, _MONTH_NAMES[ts.month - 1], ts.hour)
        if key not in lookup:
            raise ValueError(f"no EEC rate found for {ts} (key={key})")
        g, d = lookup[key]
        generation.append(g)
        delivery.append(d)
    return pd.DataFrame({"generation": generation, "delivery": delivery}, index=index)


class NBTExportPriceSeries(PriceSeries):
    """Export PriceSeries backed by a PG&E EEC table: generation + delivery halves, plus
    an optional flat ACC Plus adder ($/kWh, by interconnection-application year; e.g.
    0.0088 for 2026 residential -- see NBT Rates section D). Weekends and PG&E holidays
    get the weekend/holiday rate.

    include_produced=False drops the generation half, for CCA/Direct Access customers,
    whose generation export credit comes from their CCA, not PG&E."""

    def __init__(self, csv_path: str | Path, include_produced: bool = True, adder_per_kwh: float = 0.0):
        self.csv_path = csv_path
        self.include_produced = include_produced
        self.adder_per_kwh = adder_per_kwh

    def values(self, time_grid: TimeGrid) -> np.ndarray:
        if time_grid.start is None:
            raise ValueError("TimeGrid.start is required to look up NBT export rates")
        rates = eec_component_rates(self.csv_path, time_grid.timestamps())
        total = rates["delivery"] + (rates["generation"] if self.include_produced else 0.0)
        return total.to_numpy(dtype=float) + self.adder_per_kwh


def parse_eec_price_sheet_pdf(pdf_path: str | Path) -> dict[str, list[dict]]:
    """Parses one of PG&E's official EEC price sheet PDFs (e.g. from
    PGE-EEC-Price-Sheets.zip at https://www.pge.com/solarbilling) directly, for
    updating/regenerating a CSV like examples/data/export_rates/pge_nbt_eec_2026.csv for a
    different vintage year. Requires the optional 'pdfplumber' dependency.

    Returns {"weekday": [...], "weekend_holiday": [...]}, each a list of
    {"hour": int, "month": str, "produced": float, "delivered": float} records.

    Reconstructs the grid from word-level (x, y) coordinates rather than
    pdfplumber's extract_tables(): that heuristic merges adjacent hour rows on this
    PDF's tight ~6pt row spacing (no visible gridlines between data rows), silently
    losing rows. Raises if a table doesn't resolve to exactly 24 hour rows or has a
    missing/duplicate cell, rather than returning a silently-wrong grid.
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError(
            "parse_eec_price_sheet_pdf requires the optional 'pdfplumber' dependency: "
            "pip install pdfplumber"
        ) from exc

    with pdfplumber.open(pdf_path) as pdf:
        words = pdf.pages[0].extract_words()

    pd_headers = [w for w in words if w["text"] in ("Produced", "Delivered")]
    # "Produced"/"Delivered" also appear in the page's legend text, not just the two
    # real 24-column header rows -- cluster by y and keep only groups of exactly 24
    # (a real header row), rather than assuming the header rows are the first/last
    # distinct y-position found.
    tops_sorted = sorted(round(w["top"]) for w in pd_headers)
    y_groups: list[list[int]] = []
    current = [tops_sorted[0]]
    for y in tops_sorted[1:]:
        if y - current[-1] > 3:
            y_groups.append(current)
            current = [y]
        else:
            current.append(y)
    y_groups.append(current)
    header_rows = [group[0] for group in y_groups if len(group) == 24]
    if len(header_rows) != 2:
        raise ValueError(
            f"expected exactly 2 header rows with 24 columns each, found {len(header_rows)}: {header_rows}"
        )
    weekday_header_top, weekend_header_top = sorted(header_rows)

    def header_cols(header_top: int) -> list[float]:
        matches = sorted(
            (w for w in pd_headers if abs(w["top"] - header_top) < 3), key=lambda w: w["x0"]
        )
        if len(matches) != 24:
            raise ValueError(f"expected 24 column headers near y={header_top}, found {len(matches)}")
        return [w["x0"] for w in matches]

    def parse_table(y_top: float, y_bottom: float, col_x: list[float]) -> list[list[float]]:
        dollar_words = [
            w for w in words if w["text"].startswith("$") and y_top - 1 <= w["top"] <= y_bottom + 1
        ]
        ys = sorted({round(w["top"], 1) for w in dollar_words})
        # cluster word y-positions into 24 hour rows: a gap > 3pt starts a new row.
        # Needed because this PDF has no visible gridlines between data rows (~6pt
        # apart), which is exactly what makes pdfplumber's extract_tables() unreliable
        # here -- it merges adjacent rows instead of emitting 24 separate ones.
        rows: list[list[float]] = []
        current = [ys[0]]
        for y in ys[1:]:
            if y - current[-1] > 3.0:
                rows.append(current)
                current = [y]
            else:
                current.append(y)
        rows.append(current)
        row_centers = [sum(r) / len(r) for r in rows]
        if len(row_centers) != 24:
            raise ValueError(f"expected 24 hour rows in table at y in [{y_top},{y_bottom}], got {len(row_centers)}")

        grid: list[list[float | None]] = [[None] * 24 for _ in range(24)]
        for w in dollar_words:
            r = min(range(24), key=lambda i: abs(row_centers[i] - w["top"]))
            c = min(range(24), key=lambda i: abs(col_x[i] - w["x0"]))
            value = float(w["text"].replace("$", ""))
            if grid[r][c] is not None:
                raise ValueError(f"duplicate value at hour-row {r}, col {c}: {grid[r][c]} vs {value}")
            grid[r][c] = value
        missing = [(r, c) for r in range(24) for c in range(24) if grid[r][c] is None]
        if missing:
            raise ValueError(f"missing {len(missing)} cells, e.g. {missing[:5]}")
        return grid

    def grid_to_records(grid: list[list[float]]) -> list[dict]:
        return [
            {
                "hour": hour,
                "month": month,
                "produced": grid[hour][month_idx * 2],
                "delivered": grid[hour][month_idx * 2 + 1],
            }
            for hour in range(24)
            for month_idx, month in enumerate(_MONTH_NAMES)
        ]

    weekday_col_x = header_cols(weekday_header_top)
    weekend_col_x = header_cols(weekend_header_top)
    weekday_grid = parse_table(weekday_header_top + 5, weekday_header_top + 150, weekday_col_x)
    weekend_grid = parse_table(weekend_header_top + 5, weekend_header_top + 150, weekend_col_x)

    return {"weekday": grid_to_records(weekday_grid), "weekend_holiday": grid_to_records(weekend_grid)}
