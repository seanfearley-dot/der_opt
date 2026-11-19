"""Shared billing fixtures: a simple flat tariff and simple export rates, small enough
to check bills by hand (see tests/unit/test_billing.py's docstring)."""

import pytest

from der_opt.billing import load_retail_tariff

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


SIMPLE_TOU = """
name: simple tou
seasons: {all: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]}
periods_by_hour:
  peak: [16, 17, 18, 19, 20]
  off_peak: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 21, 22, 23]
components:
  gen: {bucket: generation, rate: {all: {peak: 0.30, off_peak: 0.10}}}
  dist: {bucket: delivery, rate: 0.20}
  cia: {bucket: delivery, rate: 0.05}
  ppp: {bucket: nbc, rate: 0.02}
base_services_charge_per_day: {tier_3: 1.0}
baseline:
  component: cia
  baseline_rate: -0.03
  published_credit_per_kwh: 0.08
  quantities_kwh_per_day:
    basic: {X: {all: 10.0}}
"""


@pytest.fixture
def tou_with_baseline(tmp_path):
    """Simple TOU tariff with a baseline credit; see tests/unit/test_billing_nem2.py."""
    path = tmp_path / "tou.yaml"
    path.write_text(SIMPLE_TOU)
    return load_retail_tariff(path)
