from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from der_opt.results.schema import DispatchResult
from der_opt.time_grid import TimeGrid
from der_opt.validation import crosscheck_annual_bill

pytest.importorskip("PySAM", reason="NREL-PySAM is an optional dependency (der-opt[validation])")

URDB_PATH = Path(__file__).parent.parent.parent / "examples" / "data" / "references/sample_tariff_urdb.json"


def _year_dispatch(import_kw: float, export_kw: float) -> DispatchResult:
    index = list(range(8760))
    return DispatchResult(
        premise_name="test",
        time_index=None,
        device_dispatch=pd.DataFrame(index=index),
        grid_import=pd.Series(np.full(8760, import_kw), index=index),
        grid_export=pd.Series(np.full(8760, export_kw), index=index),
        objective_value=0.0,
        solver_status="synthetic",
    )


def test_crosscheck_requires_a_full_year_hourly_grid():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=24)
    with pytest.raises(ValueError):
        crosscheck_annual_bill(_year_dispatch(1.0, 0.0), tg, URDB_PATH)


def test_crosscheck_runs_and_export_reduces_the_bill():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=8760)
    no_export = crosscheck_annual_bill(_year_dispatch(1.0, 0.0), tg, URDB_PATH)
    with_export = crosscheck_annual_bill(_year_dispatch(1.0, 0.5), tg, URDB_PATH)

    assert no_export["utility_bill_w_sys_year1"] > 0
    assert with_export["utility_bill_w_sys_year1"] < no_export["utility_bill_w_sys_year1"]
