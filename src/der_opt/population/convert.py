"""`der-opt convert premises`: a 1.1 premises file (device columns) to the 2.0 pair, a
premises file without them and a devices file (docs/INPUT_DATA_SPEC.md section 0).

A 1.1 premise has solar when its time series has `pv_kw` (`pv_kw_ac` is only a check),
so the time series files are read to decide. Devices keep the names 1.1 gave them
(solar, battery, ev), so counterfactual policies and rules written by name still apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from der_opt.inputs.spec import V11_DEVICE_COLUMNS


@dataclass
class Converted:
    premises: Path
    devices: Path
    counts: dict[str, int]


def _cell(row, column):
    value = row.get(column, "")
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value).strip()


def convert_premises(premises: str | Path, timeseries: list[str | Path], out_dir: str | Path) -> Converted:
    from der_opt.inputs import read_timeseries

    premises = Path(premises)
    out_dir = Path(out_dir)
    if out_dir.resolve() == premises.parent.resolve() and (out_dir / "premises.csv").resolve() == premises.resolve():
        raise ValueError("write the converted files to another folder, so the 1.1 file isn't overwritten")
    frame = pd.read_csv(premises, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    files = [read_timeseries(p) for p in timeseries]

    def has_pv(premise: str) -> bool:
        return any(ts.has("pv_kw", premise) for ts in files)

    rows = []
    for _, row in frame.iterrows():
        premise = row["premise_id"].strip()
        if has_pv(premise):
            rows.append({"premise_id": premise, "device_id": "solar", "type": "solar", "kw": _cell(row, "pv_kw_ac")})
        if _cell(row, "battery_kwh"):
            rows.append({"premise_id": premise, "device_id": "battery", "type": "battery",
                         "kw": _cell(row, "battery_kw"), "kwh": _cell(row, "battery_kwh")})
        if _cell(row, "ev_battery_kwh"):
            rows.append({"premise_id": premise, "device_id": "ev", "type": "ev", "kw": _cell(row, "ev_charger_kw"),
                         "kwh": _cell(row, "ev_battery_kwh"), "v2x": "1" if _cell(row, "ev_v2x") in ("1", "1.0") else "0"})
    out_dir.mkdir(parents=True, exist_ok=True)
    kept = frame.drop(columns=[c for c in V11_DEVICE_COLUMNS if c in frame])
    kept.to_csv(out_dir / "premises.csv", index=False)
    devices = pd.DataFrame(rows, columns=["premise_id", "device_id", "type", "kw", "kwh", "v2x"])
    devices.to_csv(out_dir / "devices.csv", index=False)
    counts = devices["type"].value_counts().to_dict()
    return Converted(out_dir / "premises.csv", out_dir / "devices.csv", counts)
