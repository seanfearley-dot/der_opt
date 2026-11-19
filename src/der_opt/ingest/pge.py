"""PG&E "Download My Data" (Green Button) electric interval CSV -> a scenario CSV.

What the reader relies on: an account preamble (name, address, account number,
service), then a header row starting with TYPE, DATE and START TIME, one row per
interval, and one or more energy columns whose names carry the unit, e.g.
"USAGE (kWh)". Rows for gas, if a file mixes them, are dropped.

What it doesn't assume: how solar export is shown. It takes separate IMPORT and
EXPORT columns if there are any, and otherwise treats USAGE as net (negative =
export) -- and says which it found. This reader was built from the published layout,
not from a real solar customer's file, so check that first file's totals against
the bill.

Nothing from the preamble is read into the output or the report: it holds the
customer's name and address.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pandas as pd

from der_opt.diagnostics import Finding
from der_opt.ingest.interval import (
    IngestResult,
    IntervalSpec,
    TimestampSpec,
    _find_header,
    ingest_frame,
)

UNIT_IN_NAME = re.compile(r"\((k?w?h|m?wh|kwh)\)", re.IGNORECASE)


def _unit(column: str) -> str:
    match = UNIT_IN_NAME.search(column)
    if not match:
        raise ValueError(f"can't tell the unit of {column!r}; expected something like 'USAGE (kWh)'")
    return match.group(1).lower()


def read_pge_interval_csv(path: str | Path, resolution_minutes: int | None = None, tz: str = "America/Los_Angeles") -> IngestResult:
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    header = _find_header(lines, ["TYPE", "DATE", "START TIME"])
    raw = pd.read_csv(io.StringIO("\n".join(lines[header:])), skipinitialspace=True)
    raw.columns = [str(c).strip() for c in raw.columns]

    note = None
    if "TYPE" in raw:
        kinds = raw["TYPE"].astype(str)
        electric = kinds.str.contains("electric", case=False)
        if not electric.any():
            raise ValueError("no electric rows (TYPE 'Electric usage'); is this a gas file?")
        if (~electric).any():
            note = f"dropped {int((~electric).sum())} non-electric rows ({', '.join(sorted(set(kinds[~electric])))})"
        raw = raw[electric]

    def column(word: str) -> str | None:
        return next((c for c in raw.columns if word in c.upper()), None)

    imported, exported, usage = column("IMPORT"), column("EXPORT"), column("USAGE")
    if imported:
        columns = {"delivered": imported} | ({"received": exported} if exported else {})
        unit, layout = _unit(imported), f"separate import ({imported}) and export ({exported}) columns"
    elif usage:
        columns, unit, layout = {"net": usage}, _unit(usage), f"one usage column ({usage}), read as net"
    else:
        raise ValueError(f"no USAGE or IMPORT column among {list(raw.columns)}")

    spec = IntervalSpec(
        path=str(path), unit=unit, columns=columns, resolution_minutes=resolution_minutes,
        timestamp=TimestampSpec(date_column="DATE", time_column="START TIME", tz=tz, convention="beginning"),
    )
    result = ingest_frame(raw, spec)
    found = [Finding("warning", "pge layout", note)] if note else []
    result.report.ran("pge layout", found)
    result.summary["layout"] = layout
    return result
