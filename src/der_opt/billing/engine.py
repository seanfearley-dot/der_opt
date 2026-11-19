"""Computes a utility bill from interval import/export, the way PG&E bills it.

Follows PG&E Schedule NBT (Net Billing Tariff) Special Condition 2 for export credits:
  - imports and exports are not netted; imports are billed on the retail tariff,
    exports earn the hourly export compensation rate (generation + delivery halves)
  - generation export credits only offset generation kWh charges and delivery credits
    only delivery kWh charges; neither offsets non-bypassable charges (NBCs) or fixed
    charges such as the base services charge (2.d, 2.f)
  - the ACC Plus adder is a separate credit that offsets any charge (2.c)
  - unused credits roll forward month to month within the 12-cycle Relevant Period;
    at true-up, any remaining excess is carried forward to the next Relevant Period
    (not forfeited), and charges overpaid earlier in the year because later credits
    couldn't reach back are credited back (2.e, 2.h)
  - Net Surplus Compensation applies at true-up if annual export kWh exceed import kWh:
    the surplus kWh are debited at PG&E's average NBT export rate and credited at the
    NSC rate (Special Condition 5.d). Both rates change monthly and aren't in the
    tariff sheet, so they're inputs here.

NEM 2.0 (PG&E Schedule NEM2) is billed by _compute_nem2_bill below; see its docstring.

Bundled-service customers only: a CCA/Direct Access customer's generation charges and
credits are billed by their CCA, which this doesn't model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from der_opt.billing.tariff import RetailTariff


@dataclass
class NBTProgram:
    """Net Billing Tariff settings for one customer.

    export_rates_csv: the export compensation rate table for the customer's
        interconnection-application vintage (e.g. examples/data/pge_nbt_eec_2026.csv).
    acc_plus_per_kwh: ACC Plus adder for that vintage (2026 residential: 0.0088).
    nsc_rate_per_kwh / nsc_debit_rate_per_kwh: Net Surplus Compensation rate and the
        average NBT export rate surplus kWh are debited at. Only used at a true-up with
        net surplus; if either is missing then, the bill notes NSC wasn't computed.
    carry_in_*: credit balances carried in from a previous Relevant Period.
    """

    export_rates_csv: str | Path
    acc_plus_per_kwh: float = 0.0
    nsc_rate_per_kwh: float | None = None
    nsc_debit_rate_per_kwh: float | None = None
    carry_in_generation: float = 0.0
    carry_in_delivery: float = 0.0
    carry_in_acc_plus: float = 0.0


@dataclass
class NEM2Program:
    """NEM 2.0 settings for one customer (PG&E Schedule NEM2, for systems that applied
    before April 15, 2023; 20 years from interconnection).

    nsc_rate_per_kwh: Net Surplus Compensation rate for the true-up month (PG&E
        publishes it monthly: a 12-month average of daytime wholesale prices). Only used
        at a true-up with net surplus; if missing then, the bill notes NSC wasn't computed.
    """

    nsc_rate_per_kwh: float | None = None


def _clean(value: float) -> float:
    """Plain float, with floating-point residue (e.g. -5e-14) snapped to zero."""
    value = float(value)
    return 0.0 if abs(value) < 1e-9 else value


@dataclass
class Bill:
    cycles: pd.DataFrame  # one row per billing cycle
    true_up: dict | None
    summary: dict
    notes: list[str] = field(default_factory=list)


def _assign_cycles(index: pd.DatetimeIndex, cycle_start_dates) -> pd.Series:
    if cycle_start_dates is None:
        return pd.Series([pd.Timestamp(ts.year, ts.month, 1) for ts in index], index=index)
    starts = sorted(pd.Timestamp(d) for d in cycle_start_dates)
    if index[0] < starts[0]:
        raise ValueError(f"interval data starts {index[0]} before the first cycle start {starts[0]}")
    positions = np.searchsorted(np.array(starts, dtype="datetime64[ns]"), index.to_numpy(), side="right") - 1
    return pd.Series([starts[p] for p in positions], index=index)


def compute_bill(
    timestamps,
    import_kwh,
    export_kwh,
    tariff: RetailTariff,
    program: NBTProgram | NEM2Program | None = None,
    income_tier: int = 3,
    cycle_start_dates: list[date] | None = None,
    relevant_period_cycles: int = 12,
    baseline_territory: str | None = None,
    baseline_code: str = "basic",
) -> Bill:
    """Bill for interval data. `timestamps` mark the start of each interval;
    `import_kwh`/`export_kwh` are energy (kWh) per interval, any interval length.

    program=None bills imports only, with no export credit. Billing cycles default to
    calendar months; pass cycle_start_dates (meter-read dates) to match a real bill.
    The base services charge is charged for each calendar day that has data, so a
    partial cycle at either end isn't charged for days outside the data.

    A true-up runs only when the data spans exactly `relevant_period_cycles` cycles;
    otherwise credit balances are reported as of the end of the data.

    baseline_territory / baseline_code: the customer's baseline territory letter and
    "basic" or "all_electric"; required if the tariff has a baseline credit.
    """
    index = pd.DatetimeIndex(timestamps)
    imports = np.asarray(import_kwh, dtype=float)
    exports = np.asarray(export_kwh, dtype=float)
    if not (len(index) == len(imports) == len(exports)):
        raise ValueError("timestamps, import_kwh, and export_kwh must have the same length")
    if (imports < -1e-9).any() or (exports < -1e-9).any():
        raise ValueError("import_kwh and export_kwh must be non-negative (they're separate meter channels)")

    tier_key = f"tier_{income_tier}"
    if tariff.base_services_charge_per_day and tier_key not in tariff.base_services_charge_per_day:
        raise ValueError(f"tariff has no base services charge for income tier {income_tier}")
    bsc_per_day = tariff.base_services_charge_per_day.get(tier_key, 0.0)

    cycle_of = _assign_cycles(index, cycle_start_dates)
    if tariff.baseline is not None and baseline_territory is None:
        raise ValueError(f"{tariff.name} has a baseline credit: pass baseline_territory (e.g. 'X')")
    if isinstance(program, NEM2Program):
        return _compute_nem2_bill(
            index, imports, exports, tariff, program, bsc_per_day, cycle_of,
            relevant_period_cycles, baseline_territory, baseline_code,
        )
    if tariff.baseline is not None and program is not None:
        raise NotImplementedError("a baseline-credit tariff under NBT isn't supported (NBT bills on E-ELEC)")

    rates = tariff.bucket_rates(index)
    frame = pd.DataFrame({
        "cycle": cycle_of,
        "date": index.normalize(),
        "import_kwh": imports,
        "export_kwh": exports,
        "generation_charges": imports * rates["generation"].to_numpy(),
        "delivery_charges": imports * rates["delivery"].to_numpy(),
        "nbc_charges": imports * rates["nbc"].to_numpy(),
    }, index=index)

    if program is not None:
        from der_opt.integrations.pge_nbt_export import eec_component_rates

        eec = eec_component_rates(program.export_rates_csv, index)
        frame["generation_credits"] = exports * eec["generation"].to_numpy()
        frame["delivery_credits"] = exports * eec["delivery"].to_numpy()
        frame["acc_plus_credits"] = exports * program.acc_plus_per_kwh
    else:
        frame["generation_credits"] = frame["delivery_credits"] = frame["acc_plus_credits"] = 0.0

    grouped = frame.groupby("cycle")
    cycles = grouped[[
        "import_kwh", "export_kwh", "generation_charges", "delivery_charges", "nbc_charges",
        "generation_credits", "delivery_credits", "acc_plus_credits",
    ]].sum()
    cycles["days"] = grouped["date"].nunique()
    cycles["base_services_charge"] = cycles["days"] * bsc_per_day
    if tariff.baseline is not None:  # no export program: a plain usage discount
        allowance = grouped["date"].agg(lambda dates: tariff.baseline.allowance_kwh(
            dates.unique(), tariff.season_by_month, baseline_territory, baseline_code
        ))
        cycles["baseline_kwh"] = np.minimum(cycles["import_kwh"], allowance)
        cycles[f"{tariff.baseline.bucket}_charges"] -= cycles["baseline_kwh"] * tariff.baseline.credit_per_kwh

    gen_balance = program.carry_in_generation if program else 0.0
    del_balance = program.carry_in_delivery if program else 0.0
    acc_balance = program.carry_in_acc_plus if program else 0.0
    rows = []
    for cycle, row in cycles.iterrows():
        gen_available = gen_balance + row["generation_credits"]
        gen_applied = min(row["generation_charges"], gen_available)
        gen_balance = gen_available - gen_applied

        del_available = del_balance + row["delivery_credits"]
        del_applied = min(row["delivery_charges"], del_available)
        del_balance = del_available - del_applied

        generation_due = row["generation_charges"] - gen_applied
        delivery_due = row["delivery_charges"] - del_applied
        subtotal = generation_due + delivery_due + row["nbc_charges"] + row["base_services_charge"]

        acc_available = acc_balance + row["acc_plus_credits"]
        acc_applied = min(subtotal, acc_available)
        acc_balance = acc_available - acc_applied

        rows.append({
            "cycle_start": cycle,
            "generation_credits_applied": gen_applied,
            "delivery_credits_applied": del_applied,
            "generation_due": generation_due,
            "delivery_due": delivery_due,
            "acc_plus_applied": acc_applied,
            "amount_due": subtotal - acc_applied,
            "generation_credit_balance": gen_balance,
            "delivery_credit_balance": del_balance,
            "acc_plus_balance": acc_balance,
        })
    applied = pd.DataFrame(rows).set_index("cycle_start")
    cycles = cycles.join(applied)
    cycles.index.name = "cycle_start"

    notes: list[str] = []
    billed = float(cycles["amount_due"].sum())
    true_up = None
    if program is not None and len(cycles) == relevant_period_cycles:
        true_up = _true_up(cycles, program, gen_balance, del_balance, notes)
    elif program is not None:
        notes.append(
            f"No true-up: the data spans {len(cycles)} billing cycle(s), not a full "
            f"{relevant_period_cycles}-cycle Relevant Period. Credit balances are as of the end of the data."
        )

    if true_up is not None:
        net_cost = billed - true_up["overpayment_credit"] - true_up["nsc_credit"]
        unused = {
            "generation": true_up["generation_carry_forward"],
            "delivery": true_up["delivery_carry_forward"],
            "acc_plus": acc_balance,
        }
    else:
        net_cost = billed
        unused = {"generation": gen_balance, "delivery": del_balance, "acc_plus": acc_balance}

    summary = {
        "import_kwh": _clean(cycles["import_kwh"].sum()),
        "export_kwh": _clean(cycles["export_kwh"].sum()),
        "billed": _clean(billed),
        "net_cost": _clean(net_cost),
        "unused_credits": {key: _clean(value) for key, value in unused.items()},
    }
    return Bill(cycles=cycles, true_up=true_up, summary=summary, notes=notes)


def _true_up(cycles: pd.DataFrame, program: NBTProgram, gen_balance: float, del_balance: float, notes: list[str]) -> dict:
    """Relevant Period settlement (NBT Special Condition 2.h). Per bucket, what was owed
    for the year is charges minus credits (incl. carried-in), floored at zero; anything
    paid monthly beyond that -- because credits earned later in the year couldn't reach
    back to earlier bills -- is credited back on future bills, and leftover credit is
    carried forward. (The rolling balance always equals overpayment + carry-forward.)"""
    gen_owed = max(0.0, cycles["generation_charges"].sum() - cycles["generation_credits"].sum() - program.carry_in_generation)
    del_owed = max(0.0, cycles["delivery_charges"].sum() - cycles["delivery_credits"].sum() - program.carry_in_delivery)
    overpayment = (cycles["generation_due"].sum() - gen_owed) + (cycles["delivery_due"].sum() - del_owed)
    gen_carry = gen_balance - (cycles["generation_due"].sum() - gen_owed)
    del_carry = del_balance - (cycles["delivery_due"].sum() - del_owed)

    surplus_kwh = max(0.0, cycles["export_kwh"].sum() - cycles["import_kwh"].sum())
    nsc_debit = nsc_credit = 0.0
    if surplus_kwh > 0:
        if program.nsc_rate_per_kwh is None or program.nsc_debit_rate_per_kwh is None:
            notes.append(
                f"Net surplus of {surplus_kwh:.0f} kWh, but NSC wasn't computed: pass nsc_rate_per_kwh "
                "and nsc_debit_rate_per_kwh (both published monthly by PG&E)."
            )
        else:
            # The tariff says the debit comes from excess credits but not which bucket
            # first; this takes it from generation, then delivery.
            nsc_debit = surplus_kwh * program.nsc_debit_rate_per_kwh
            from_generation = min(nsc_debit, gen_carry)
            from_delivery = min(nsc_debit - from_generation, del_carry)
            gen_carry -= from_generation
            del_carry -= from_delivery
            if nsc_debit - from_generation - from_delivery > 1e-9:
                notes.append(
                    f"NSC debit (${nsc_debit:.2f}) exceeds the excess credits available "
                    f"(${from_generation + from_delivery:.2f}); the tariff doesn't say how the "
                    "remainder is handled, so it isn't charged here."
                )
            nsc_credit = surplus_kwh * program.nsc_rate_per_kwh

    return {key: _clean(value) for key, value in {
        "generation_owed": gen_owed,
        "delivery_owed": del_owed,
        "overpayment_credit": overpayment,
        "generation_carry_forward": gen_carry,
        "delivery_carry_forward": del_carry,
        "net_surplus_kwh": surplus_kwh,
        "nsc_debit": nsc_debit,
        "nsc_credit": nsc_credit,
    }.items()}


def bill_from_dispatch(dispatch, time_grid, tariff: RetailTariff, program: NBTProgram | NEM2Program | None = None, **kwargs) -> Bill:
    """compute_bill for a DispatchResult (optimal or counterfactual): converts its
    per-period grid import/export power (kW) to energy (kWh) for the period length."""
    if time_grid.start is None:
        raise ValueError("billing needs TimeGrid.start (real dates set the season, cycle, and day type)")
    dt = time_grid.dt_hours
    return compute_bill(
        time_grid.timestamps(),
        dispatch.grid_import.to_numpy() * dt,
        dispatch.grid_export.to_numpy() * dt,
        tariff,
        program,
        **kwargs,
    )


def _compute_nem2_bill(
    index: pd.DatetimeIndex,
    imports: np.ndarray,
    exports: np.ndarray,
    tariff: RetailTariff,
    program: NEM2Program,
    bsc_per_day: float,
    cycle_of: pd.Series,
    relevant_period_cycles: int,
    baseline_territory: str | None,
    baseline_code: str,
) -> Bill:
    """NEM 2.0, per PG&E Schedule NEM2 Special Conditions 2 and 5:
      - each billing cycle, imports and exports are netted within each discrete TOU period
        (season + period); net kWh are billed, or credited, at that period's rate (2.b),
        excluding the non-bypassable charges
      - non-bypassable charges (public purpose, nuclear decommissioning, competition
        transition, wildfire fund) are paid on each interval's imports net of that
        interval's exports, and no export credit reduces them (2.c)
      - with a baseline-credit rate, the cycle's net kWh up to the baseline allowance get
        the baseline rate: a net consumer's charges drop by the credit, and a net
        generator's credit is valued at the baseline rate too, so it drops by the same
        amount (2.a)
      - the base services charge is paid monthly; energy charges accrue and are settled at
        the true-up after 12 cycles (2.f, 2.h). Paying them monthly instead gives the same
        total: overpayments are credited back at the true-up
      - at the true-up, leftover credit is set to zero, and if exports exceed imports for
        the year, the surplus kWh are paid the NSC rate (2.h, 5)
    """
    rates = tariff.bucket_rates(index)
    frame = pd.DataFrame({
        "cycle": cycle_of,
        "date": index.normalize(),
        "tou_period": [f"{tariff.season_by_month[ts.month]}_{tariff.period_by_hour[ts.hour]}" for ts in index],
        "import_kwh": imports,
        "export_kwh": exports,
        "net_kwh": imports - exports,
        "energy_rate": (rates["generation"] + rates["delivery"]).to_numpy(),
        "nbc_charges": np.maximum(imports - exports, 0.0) * rates["nbc"].to_numpy(),
    }, index=index)

    by_period = frame.groupby(["cycle", "tou_period"]).agg(
        net_kwh=("net_kwh", "sum"), rate_min=("energy_rate", "min"), rate_max=("energy_rate", "max")
    )
    if ((by_period["rate_max"] - by_period["rate_min"]).abs() > 1e-9).any():
        raise ValueError("energy rate varies within a TOU period; NEM2 netting needs one rate per period")
    by_period["energy_charges"] = by_period["net_kwh"] * by_period["rate_min"]

    grouped = frame.groupby("cycle")
    cycles = grouped[["import_kwh", "export_kwh", "net_kwh", "nbc_charges"]].sum()
    cycles = cycles.join(by_period["net_kwh"].unstack("tou_period").add_prefix("net_kwh_").fillna(0.0))
    cycles["energy_charges"] = by_period["energy_charges"].groupby(level="cycle").sum()
    cycles["days"] = grouped["date"].nunique()
    cycles["base_services_charge"] = cycles["days"] * bsc_per_day

    if tariff.baseline is not None:
        allowance = grouped["date"].agg(lambda dates: tariff.baseline.allowance_kwh(
            dates.unique(), tariff.season_by_month, baseline_territory, baseline_code
        ))
        cycles["baseline_allowance_kwh"] = allowance
        cycles["baseline_kwh"] = np.clip(cycles["net_kwh"], -allowance, allowance)
        cycles["baseline_adjustment"] = -tariff.baseline.credit_per_kwh * cycles["baseline_kwh"]
    else:
        cycles["baseline_adjustment"] = 0.0
    cycles["energy_due"] = cycles["energy_charges"] + cycles["baseline_adjustment"]
    cycles["energy_balance"] = cycles["energy_due"].cumsum()
    cycles["amount_due"] = cycles["nbc_charges"] + cycles["base_services_charge"]
    cycles.index.name = "cycle_start"

    notes: list[str] = []
    billed = float(cycles["amount_due"].sum())
    balance = float(cycles["energy_balance"].iloc[-1])
    true_up = None
    if len(cycles) == relevant_period_cycles:
        surplus_kwh = max(0.0, float(cycles["export_kwh"].sum() - cycles["import_kwh"].sum()))
        nsc_credit = 0.0
        if surplus_kwh > 0:
            if program.nsc_rate_per_kwh is None:
                notes.append(
                    f"Net surplus of {surplus_kwh:.0f} kWh, but NSC wasn't computed: pass "
                    "nsc_rate_per_kwh (published monthly by PG&E)."
                )
            else:
                nsc_credit = surplus_kwh * program.nsc_rate_per_kwh
        true_up = {key: _clean(value) for key, value in {
            "energy_owed": max(0.0, balance),
            "credit_zeroed": max(0.0, -balance),
            "net_surplus_kwh": surplus_kwh,
            "nsc_credit": nsc_credit,
        }.items()}
        net_cost = billed + true_up["energy_owed"] - nsc_credit
        unused = 0.0  # zeroed, not carried forward
    else:
        notes.append(
            f"No true-up: the data spans {len(cycles)} billing cycle(s), not a full "
            f"{relevant_period_cycles}-cycle Relevant Period. Net energy charges accrued so far "
            f"are ${balance:.2f}; the bill counts them if positive, and a credit as $0."
        )
        net_cost = billed + max(0.0, balance)
        unused = max(0.0, -balance)

    summary = {
        "import_kwh": _clean(cycles["import_kwh"].sum()),
        "export_kwh": _clean(cycles["export_kwh"].sum()),
        "billed": _clean(billed),
        "net_cost": _clean(net_cost),
        "unused_credits": {"energy": _clean(unused)},
    }
    return Bill(cycles=cycles, true_up=true_up, summary=summary, notes=notes)
