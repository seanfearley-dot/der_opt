"""NEM 2.0 billing, against bills small enough to compute by hand. The simple TOU tariff:
energy (generation + delivery, incl. the over-baseline adjustment) $0.35/kWh off-peak and
$0.55/kWh peak (4-9 pm); non-bypassable $0.02/kWh; $1/day; baseline credit
$0.05 - (-$0.03) = $0.08/kWh on up to 10 kWh/day (territory X)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from der_opt.billing import NEM2Program, compute_bill, load_retail_tariff
from tests.unit.conftest import SIMPLE_TOU

E_TOU_C = Path(__file__).parent.parent.parent / "examples" / "data" / "tariffs" / "pge_e_tou_c_2026-06.yaml"

MONDAY = datetime(2026, 6, 1)


@pytest.fixture
def tou(tou_with_baseline):
    return tou_with_baseline


def _day(imports_by_hour: dict[int, float], exports_by_hour: dict[int, float], start=MONDAY):
    timestamps = [start + timedelta(hours=h) for h in range(24)]
    imports = np.array([imports_by_hour.get(h, 0.0) for h in range(24)])
    exports = np.array([exports_by_hour.get(h, 0.0) for h in range(24)])
    return timestamps, imports, exports


def _bill(tou, timestamps, imports, exports, program=None, **kwargs):
    kwargs.setdefault("baseline_territory", "X")
    return compute_bill(timestamps, imports, exports, tou, program or NEM2Program(), **kwargs)


def test_nets_within_each_tou_period_and_charges_nbcs_on_imports(tou):
    # off-peak: 20 kWh in, 12 out -> net 8; peak: 5 in
    ts, imp, exp = _day({h: 2.0 for h in range(10)} | {h: 1.0 for h in range(16, 21)}, {h: 3.0 for h in range(10, 14)})
    bill = _bill(tou, ts, imp, exp)
    cycle = bill.cycles.iloc[0]
    assert cycle["net_kwh_all_off_peak"] == pytest.approx(8)
    assert cycle["energy_charges"] == pytest.approx(8 * 0.35 + 5 * 0.55)
    assert cycle["baseline_adjustment"] == pytest.approx(-10 * 0.08)  # net 13 kWh, 10 at baseline
    assert cycle["nbc_charges"] == pytest.approx(25 * 0.02)  # all 25 kWh imported, not net
    assert bill.summary["net_cost"] == pytest.approx(0.5 + 1 + 5.55 - 0.8)


def test_peak_exports_are_credited_at_the_peak_rate(tou):
    # 10 kWh in off-peak, 10 kWh out at peak: zero net kWh, but a $2 credit
    ts, imp, exp = _day({h: 1.0 for h in range(10)}, {h: 2.0 for h in range(16, 21)})
    bill = _bill(tou, ts, imp, exp)
    assert bill.cycles.iloc[0]["energy_due"] == pytest.approx(10 * 0.35 - 10 * 0.55)
    # no true-up yet: the credit isn't money in hand, so it counts as $0
    assert bill.summary["net_cost"] == pytest.approx(10 * 0.02 + 1)
    assert bill.summary["unused_credits"]["energy"] == pytest.approx(2.0)


def test_net_generation_is_credited_at_the_baseline_rate_up_to_the_allowance(tou):
    ts, imp, exp = _day({}, {h: 2.0 for h in range(10)})  # 20 kWh net generation
    cycle = _bill(tou, ts, imp, exp).cycles.iloc[0]
    assert cycle["baseline_kwh"] == pytest.approx(-10)
    assert cycle["energy_due"] == pytest.approx(-20 * 0.35 + 10 * 0.08)


def test_nbcs_are_on_imports_net_of_same_interval_exports(tou):
    ts, imp, exp = _day({3: 2.0}, {3: 0.5})
    assert _bill(tou, ts, imp, exp).cycles.iloc[0]["nbc_charges"] == pytest.approx(1.5 * 0.02)


def _two_day_true_up(tou, day2_export_kwh, nsc=None):
    ts1, imp1, exp1 = _day({h: 1.0 for h in range(10)}, {})  # 10 kWh in
    ts2, imp2, exp2 = _day({}, {h: day2_export_kwh / 10 for h in range(10)}, start=MONDAY + timedelta(days=1))
    return _bill(
        tou, ts1 + ts2, np.concatenate([imp1, imp2]), np.concatenate([exp1, exp2]),
        program=NEM2Program(nsc_rate_per_kwh=nsc),
        cycle_start_dates=[date(2026, 6, 1), date(2026, 6, 2)], relevant_period_cycles=2,
    )


def test_true_up_zeroes_leftover_credit_and_pays_nsc_on_surplus_kwh(tou):
    bill = _two_day_true_up(tou, day2_export_kwh=30, nsc=0.03)
    # day 1: 3.50 - 0.80; day 2: -10.50 + 0.80 -> balance -7.00
    assert bill.true_up["energy_owed"] == 0
    assert bill.true_up["credit_zeroed"] == pytest.approx(7.0)
    assert bill.true_up["nsc_credit"] == pytest.approx(20 * 0.03)
    assert bill.summary["net_cost"] == pytest.approx(10 * 0.02 + 2 - 0.6)
    assert bill.summary["unused_credits"]["energy"] == 0  # zeroed, not carried


def test_true_up_charges_a_positive_energy_balance(tou):
    bill = _two_day_true_up(tou, day2_export_kwh=5)
    # day 1: 2.70; day 2: -1.75 + 0.40
    assert bill.true_up["energy_owed"] == pytest.approx(1.35)
    assert bill.summary["net_cost"] == pytest.approx(10 * 0.02 + 2 + 1.35)


def test_nsc_without_a_rate_is_noted(tou):
    bill = _two_day_true_up(tou, day2_export_kwh=30)
    assert any("NSC wasn't computed" in note for note in bill.notes)


def test_baseline_tariff_needs_a_territory(tou):
    ts, imp, exp = _day({0: 1.0}, {})
    with pytest.raises(ValueError, match="baseline_territory"):
        compute_bill(ts, imp, exp, tou, NEM2Program())


def test_baseline_credit_without_an_export_program(tou):
    ts, imp, exp = _day({h: 1.5 for h in range(10)}, {})  # 15 kWh, 10 at baseline
    bill = compute_bill(ts, imp, exp, tou, None, baseline_territory="X")
    assert bill.summary["net_cost"] == pytest.approx(15 * 0.37 - 10 * 0.08 + 1)


def test_e_tou_c_loads_with_the_published_baseline_credit():
    tariff = load_retail_tariff(E_TOU_C)
    assert tariff.baseline.credit_per_kwh == pytest.approx(0.08140)
    assert tariff.baseline.bucket == "delivery"
    assert tariff.total_rate([datetime(2026, 7, 1, 17)])[0] == pytest.approx(0.52240)


def test_loader_rejects_a_baseline_credit_that_doesnt_match_the_sheet(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(SIMPLE_TOU.replace("published_credit_per_kwh: 0.08", "published_credit_per_kwh: 0.09"))
    with pytest.raises(ValueError, match="baseline credit"):
        load_retail_tariff(path)
