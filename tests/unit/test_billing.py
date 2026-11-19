"""Billing engine tests, against bills small enough to compute by hand. The simple
tariff: generation $0.10/kWh, delivery $0.20/kWh, non-bypassable $0.02/kWh, base
services charge $1/day. The simple export rates: generation $0.10/kWh and delivery
$0.01/kWh on weekdays; generation $0.50/kWh on weekends/holidays."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from der_opt.billing import (
    NBTProgram,
    RetailTariffPriceSeries,
    compute_bill,
    is_weekend_or_holiday,
    load_retail_tariff,
    pge_holidays,
)
from der_opt.integrations.pge_nbt_export import eec_component_rates
from der_opt.time_grid import TimeGrid

E_ELEC = Path(__file__).parent.parent.parent / "examples" / "data" / "tariffs" / "pge_e_elec_2026-06.yaml"

SIMPLE_TARIFF = """
name: simple
seasons: {all: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]}
periods_by_hour: {flat: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]}
components:
  gen: {bucket: generation, rate: 0.10}
  dist: {bucket: delivery, rate: 0.20}
  ppp: {bucket: nbc, rate: 0.02}
base_services_charge_per_day: {tier_3: 1.0}
"""

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


@pytest.fixture
def simple_tariff(tmp_path):
    path = tmp_path / "simple.yaml"
    path.write_text(SIMPLE_TARIFF)
    return load_retail_tariff(path)


@pytest.fixture
def simple_export_rates(tmp_path):
    path = tmp_path / "eec.csv"
    lines = ["day_type,month,hour,produced_usd_per_kwh,delivered_usd_per_kwh,total_usd_per_kwh"]
    for month in MONTHS:
        for hour in range(24):
            lines.append(f"weekday,{month},{hour},0.10,0.01,0.11")
            lines.append(f"weekend_holiday,{month},{hour},0.50,0.01,0.51")
    path.write_text("\n".join(lines))
    return path


def _hours(start: datetime, n: int) -> list[datetime]:
    return [start + timedelta(hours=h) for h in range(n)]


# --- tariff ---------------------------------------------------------------------

def test_e_elec_loads_and_matches_published_totals():
    tariff = load_retail_tariff(E_ELEC)
    rates = tariff.total_rate([
        datetime(2026, 7, 15, 18), datetime(2026, 7, 15, 15), datetime(2026, 7, 15, 3),
        datetime(2026, 1, 15, 18), datetime(2026, 1, 15, 22), datetime(2026, 1, 15, 3),
    ])
    assert rates.tolist() == pytest.approx([0.55214, 0.39026, 0.33358, 0.32063, 0.29854, 0.28468])


def test_loader_rejects_components_that_dont_match_published_totals(tmp_path):
    path = tmp_path / "typo.yaml"
    path.write_text(SIMPLE_TARIFF + "published_totals: {all: {flat: 0.33}}\n")  # real sum is 0.32
    with pytest.raises(ValueError, match="don't add up"):
        load_retail_tariff(path)


def test_loader_rejects_hours_not_covered(tmp_path):
    path = tmp_path / "gap.yaml"
    path.write_text(SIMPLE_TARIFF.replace(", 23]}", "]}"))
    with pytest.raises(ValueError, match="hours 0-23"):
        load_retail_tariff(path)


def test_retail_tariff_price_series_is_the_total_rate():
    tariff = load_retail_tariff(E_ELEC)
    tg = TimeGrid(resolution_minutes=60, horizon_steps=24, start=datetime(2026, 7, 15))
    values = RetailTariffPriceSeries(tariff).values(tg)
    assert values[18] == pytest.approx(0.55214)
    assert values[3] == pytest.approx(0.33358)


# --- holidays -------------------------------------------------------------------

def test_pge_holidays_2026():
    assert pge_holidays(2026) == {
        date(2026, 1, 1), date(2026, 2, 16), date(2026, 5, 25),
        date(2026, 7, 3),  # July 4 is a Saturday -> observed Friday
        date(2026, 9, 7), date(2026, 11, 11), date(2026, 11, 26), date(2026, 12, 25),
    }


def test_saturday_new_years_is_observed_the_prior_year():
    assert date(2022, 1, 1).weekday() == 5
    assert is_weekend_or_holiday(date(2021, 12, 31))


def test_export_rates_use_weekend_rate_on_holidays(simple_export_rates):
    rates = eec_component_rates(simple_export_rates, [datetime(2026, 7, 2, 12), datetime(2026, 7, 3, 12)])
    assert rates["generation"].tolist() == pytest.approx([0.10, 0.50])  # Thu weekday, Fri observed holiday


# --- bills without export credit -------------------------------------------------

def test_no_program_bills_imports_and_daily_charge(simple_tariff):
    ts = _hours(datetime(2026, 3, 2), 48)  # 2 days
    bill = compute_bill(ts, [1.0] * 48, [5.0] * 48, simple_tariff)
    assert bill.summary["billed"] == pytest.approx(48 * 0.32 + 2 * 1.0)  # exports earn nothing


def test_base_services_charge_is_per_day_with_data(simple_tariff):
    ts = _hours(datetime(2026, 3, 2), 24 * 3)
    bill = compute_bill(ts, [0.0] * 72, [0.0] * 72, simple_tariff)
    assert bill.summary["billed"] == pytest.approx(3.0)


# --- NBT credit rules --------------------------------------------------------------

def test_export_credits_only_offset_their_own_bucket(simple_tariff, simple_export_rates):
    # Mon 2026-03-02, 10 hours importing 1 kWh then 10 hours exporting 100 kWh.
    ts = _hours(datetime(2026, 3, 2), 20)
    imports = [1.0] * 10 + [0.0] * 10
    exports = [0.0] * 10 + [100.0] * 10
    bill = compute_bill(ts, imports, exports, simple_tariff, NBTProgram(simple_export_rates))
    cycle = bill.cycles.iloc[0]

    # generation: $1.00 of charges, $100 of credits -> fully offset, $99 left over
    assert cycle["generation_due"] == pytest.approx(0.0)
    assert cycle["generation_credit_balance"] == pytest.approx(99.0)
    # delivery: $2.00 of charges, $10 of delivery credits -> fully offset, $8 left;
    # the $99 of leftover *generation* credit never touches delivery
    assert cycle["delivery_due"] == pytest.approx(0.0)
    assert cycle["delivery_credit_balance"] == pytest.approx(8.0)
    # NBCs ($0.20) and the base services charge ($1.00) can't be offset at all
    assert cycle["amount_due"] == pytest.approx(0.20 + 1.0)


def test_generation_credit_cannot_cover_a_delivery_shortfall(simple_tariff, simple_export_rates):
    ts = _hours(datetime(2026, 3, 2), 20)
    imports = [10.0] * 10 + [0.0] * 10  # $10 gen, $20 delivery, $2 NBC
    exports = [0.0] * 10 + [100.0] * 10  # $100 gen credit, $10 delivery credit
    bill = compute_bill(ts, imports, exports, simple_tariff, NBTProgram(simple_export_rates))
    cycle = bill.cycles.iloc[0]
    assert cycle["delivery_due"] == pytest.approx(10.0)  # $20 - $10, despite $90 spare gen credit
    assert cycle["amount_due"] == pytest.approx(10.0 + 2.0 + 1.0)


def test_acc_plus_offsets_nbcs_and_the_base_services_charge(simple_tariff, simple_export_rates):
    ts = _hours(datetime(2026, 3, 2), 20)
    imports = [1.0] * 10 + [0.0] * 10
    exports = [0.0] * 10 + [100.0] * 10
    program = NBTProgram(simple_export_rates, acc_plus_per_kwh=0.01)  # $10 of ACC Plus
    bill = compute_bill(ts, imports, exports, simple_tariff, program)
    cycle = bill.cycles.iloc[0]
    assert cycle["acc_plus_applied"] == pytest.approx(1.20)  # covers the NBC + base charge
    assert cycle["amount_due"] == pytest.approx(0.0)
    assert cycle["acc_plus_balance"] == pytest.approx(10.0 - 1.20)


def test_unused_credits_roll_into_the_next_cycle(simple_tariff, simple_export_rates):
    march = _hours(datetime(2026, 3, 30), 10)   # Mon: export $10 of gen credit, no import
    april = _hours(datetime(2026, 4, 1), 10)    # Wed: import $5 of gen charges
    bill = compute_bill(
        march + april, [0.0] * 10 + [5.0] * 10, [10.0] * 10 + [0.0] * 10,
        simple_tariff, NBTProgram(simple_export_rates),
    )
    assert bill.cycles["generation_due"].tolist() == pytest.approx([0.0, 0.0])
    assert bill.cycles["generation_credit_balance"].tolist() == pytest.approx([10.0, 5.0])


def test_cycle_start_dates_split_cycles_on_meter_read_days(simple_tariff):
    ts = _hours(datetime(2026, 3, 10), 24 * 10)  # Mar 10-19
    bill = compute_bill(ts, [1.0] * 240, [0.0] * 240, simple_tariff,
                        cycle_start_dates=[date(2026, 3, 1), date(2026, 3, 15)])
    assert bill.cycles["days"].tolist() == [5, 5]


# --- true-up ---------------------------------------------------------------------

def test_true_up_credits_back_charges_paid_before_later_credits(simple_tariff, simple_export_rates):
    # Cycle 1: pay $10 of generation charges. Cycle 2: earn $30 of generation credit.
    # Over the period, credits exceed charges: owed $0, so the $10 paid in cycle 1 is
    # credited back and $20 carries forward.
    cycle1 = _hours(datetime(2026, 3, 2), 10)
    cycle2 = _hours(datetime(2026, 4, 1), 10)
    bill = compute_bill(
        cycle1 + cycle2, [10.0] * 10 + [0.0] * 10, [0.0] * 10 + [30.0] * 10,
        simple_tariff, NBTProgram(simple_export_rates), relevant_period_cycles=2,
    )
    assert bill.true_up["generation_owed"] == pytest.approx(0.0)
    assert bill.true_up["generation_carry_forward"] == pytest.approx(20.0)
    # overpayment covers both buckets: $10 generation, plus $20 delivery paid in
    # cycle 1 less the $3 of delivery credit earned in cycle 2
    assert bill.true_up["overpayment_credit"] == pytest.approx(10.0 + 3.0)
    assert bill.summary["net_cost"] == pytest.approx(bill.summary["billed"] - 13.0)


def test_rolling_balance_equals_overpayment_plus_carry_forward(simple_tariff, simple_export_rates):
    ts = _hours(datetime(2026, 3, 2), 10) + _hours(datetime(2026, 4, 1), 10)
    imports = [3.0, 0.0] * 10
    exports = [0.0, 7.0] * 10
    bill = compute_bill(ts, imports, exports, simple_tariff, NBTProgram(simple_export_rates), relevant_period_cycles=2)
    end = bill.cycles.iloc[-1]
    assert end["generation_credit_balance"] + end["delivery_credit_balance"] == pytest.approx(
        bill.true_up["overpayment_credit"]
        + bill.true_up["generation_carry_forward"]
        + bill.true_up["delivery_carry_forward"]
    )


def test_net_surplus_compensation(simple_tariff, simple_export_rates):
    ts = _hours(datetime(2026, 3, 2), 20)
    imports = [1.0] * 10 + [0.0] * 10    # 10 kWh in
    exports = [0.0] * 10 + [10.0] * 10   # 100 kWh out -> 90 kWh net surplus
    program = NBTProgram(simple_export_rates, nsc_rate_per_kwh=0.04, nsc_debit_rate_per_kwh=0.08)
    bill = compute_bill(ts, imports, exports, simple_tariff, program, relevant_period_cycles=1)
    assert bill.true_up["net_surplus_kwh"] == pytest.approx(90.0)
    assert bill.true_up["nsc_debit"] == pytest.approx(90 * 0.08)
    assert bill.true_up["nsc_credit"] == pytest.approx(90 * 0.04)
    # generation: $10 credit - $1 charges = $9 excess, less the $7.20 debit
    assert bill.true_up["generation_carry_forward"] == pytest.approx(9.0 - 7.2)


def test_net_surplus_without_rates_is_noted_not_guessed(simple_tariff, simple_export_rates):
    ts = _hours(datetime(2026, 3, 2), 20)
    bill = compute_bill(ts, [1.0] * 10 + [0.0] * 10, [0.0] * 10 + [10.0] * 10,
                        simple_tariff, NBTProgram(simple_export_rates), relevant_period_cycles=1)
    assert bill.true_up["nsc_credit"] == 0.0
    assert any("NSC wasn't computed" in note for note in bill.notes)


def test_no_true_up_for_a_partial_relevant_period(simple_tariff, simple_export_rates):
    ts = _hours(datetime(2026, 3, 2), 24)
    bill = compute_bill(ts, [1.0] * 24, [0.0] * 24, simple_tariff, NBTProgram(simple_export_rates))
    assert bill.true_up is None
    assert any("No true-up" in note for note in bill.notes)


def test_rejects_negative_channels(simple_tariff):
    with pytest.raises(ValueError):
        compute_bill(_hours(datetime(2026, 3, 2), 2), [1.0, -1.0], [0.0, 0.0], simple_tariff)
