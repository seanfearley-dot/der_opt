"""The NBT bill as an optimization objective.

EnergyArbitrage values every exported kWh at its full export rate. On an NBT bill that's
only true while there are charges of the same kind left to offset: generation credit
offsets only generation charges, delivery credit only delivery charges, and neither
touches non-bypassable charges or the base services charge. A household that exports
more than it uses strands the rest. NBTBill puts those limits in the objective, so
the optimizer minimizes the bill der_opt.billing.compute_bill would produce.

It stays a linear program (so duals stay exact). Per billing cycle and bucket, an
"applied" variable is bounded by that cycle's charges and by the credit available so
far; minimizing the bill fills it as far as the rules allow, which is exactly what the
bill engine does greedily:

  - no true-up (fewer cycles than a Relevant Period): credit applied through cycle m
    can't exceed carry-in plus credit earned through cycle m (rollover only goes
    forward in time)
  - true-up (exactly a Relevant Period): credit earned any time in the year can offset
    any month's charges of its bucket, because the true-up credits back what later
    credits would have covered
  - ACC Plus offsets any charge, rolling forward month to month (no true-up refund)
  - Net Surplus Compensation: s <= annual export kWh - import kWh, s >= 0, credited at
    the NSC rate. Since NSC is a credit, minimizing the bill pushes s to the surplus.

Leftover credit at the end is valued at zero: the objective is this period's net cost,
the same `net_cost` the bill engine reports. That's right for a household that is a net
exporter every year (carried-forward credit never gets used); it understates exports
for a household that would use the credit next year, and for a run shorter than a
Relevant Period, where credit left at the end would offset bills after the horizon.

One edge case differs from the bill engine: with a true-up, the engine applies ACC Plus
against each month's bill before true-up adjustments; here it's applied against the
year's bill allocated across months. They only diverge if ACC Plus credit exceeds a
month's bill, which at 2026 rates means exporting thousands of kWh in one month.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pyomo.environ as pyo

from der_opt.billing.engine import NBTProgram, _assign_cycles, compute_bill
from der_opt.billing.tariff import RetailTariff
from der_opt.premise.objective import ValueStream
from der_opt.time_grid import TimeGrid

if TYPE_CHECKING:
    from der_opt.premise.premise import Premise


class NBTBill(ValueStream):
    """Cost = the customer's bill (net of true-up and NSC) for the dispatch's import and
    export. Replaces EnergyArbitrage; don't use both, or energy is counted twice.
    program=None bills imports only. Arguments match der_opt.billing.compute_bill."""

    def __init__(
        self,
        tariff: RetailTariff,
        program: NBTProgram | None,
        time_grid: TimeGrid,
        income_tier: int = 3,
        cycle_start_dates=None,
        relevant_period_cycles: int = 12,
    ):
        if time_grid.start is None:
            raise ValueError("NBTBill needs TimeGrid.start (real dates set the season, cycle, and day type)")
        self.tariff = tariff
        self.program = program
        self.bill_kwargs = {
            "income_tier": income_tier,
            "cycle_start_dates": cycle_start_dates,
            "relevant_period_cycles": relevant_period_cycles,
        }
        index = pd.DatetimeIndex(time_grid.timestamps())
        dt = time_grid.dt_hours

        # $ per kW held for one period, per bucket
        rates = tariff.bucket_rates(index)
        self._charge = {bucket: rates[bucket].to_numpy() * dt for bucket in ("generation", "delivery", "nbc")}
        if program is not None:
            from der_opt.integrations.pge_nbt_export import eec_component_rates

            eec = eec_component_rates(program.export_rates_csv, index)
            self._credit = {bucket: eec[bucket].to_numpy() * dt for bucket in ("generation", "delivery")}
            self._acc_credit = np.full(len(index), program.acc_plus_per_kwh * dt)
        else:
            zeros = np.zeros(len(index))
            self._credit = {"generation": zeros, "delivery": zeros}
            self._acc_credit = zeros
        self._dt = dt

        cycle_of = _assign_cycles(index, cycle_start_dates).to_numpy()
        labels = sorted(set(cycle_of))
        self._cycle_periods = [np.flatnonzero(cycle_of == label).tolist() for label in labels]

        tier_key = f"tier_{income_tier}"
        if tariff.base_services_charge_per_day and tier_key not in tariff.base_services_charge_per_day:
            raise ValueError(f"tariff has no base services charge for income tier {income_tier}")
        bsc_per_day = tariff.base_services_charge_per_day.get(tier_key, 0.0)
        dates = index.normalize()
        self._bsc = [bsc_per_day * len(set(dates[periods])) for periods in self._cycle_periods]

        self._true_up = program is not None and len(labels) == relevant_period_cycles
        self._nsc = (
            self._true_up
            and program.nsc_rate_per_kwh is not None
            and program.nsc_debit_rate_per_kwh is not None
        )
        self._cost = None

    def apply(self, model: pyo.ConcreteModel, premise: Premise) -> None:
        imp, exp = premise.grid_import, premise.grid_export
        cycles = range(len(self._cycle_periods))
        name = f"{premise.name}_nbt_bill"
        program = self.program

        def cycle_sum(rate, var, m):
            return sum(rate[t] * var[t] for t in self._cycle_periods[m])

        def add(suffix, component):
            model.add_component(f"{name}_{suffix}", component)
            return component

        charges = {
            bucket: add(f"{bucket}_charges", pyo.Expression(cycles, rule=lambda _m, m, b=bucket: cycle_sum(self._charge[b], imp, m)))
            for bucket in ("generation", "delivery", "nbc")
        }
        applied = {}
        for bucket in ("generation", "delivery"):
            credits = add(f"{bucket}_credits", pyo.Expression(
                cycles, rule=lambda _m, m, b=bucket: cycle_sum(self._credit[b], exp, m)
            ))
            carry_in = getattr(program, f"carry_in_{bucket}") if program is not None else 0.0
            used = add(f"{bucket}_applied", pyo.Var(cycles, domain=pyo.NonNegativeReals))
            add(f"{bucket}_applied_le_charges", pyo.Constraint(
                cycles, rule=lambda _m, m, u=used, c=charges[bucket]: u[m] <= c[m]
            ))
            if self._true_up:
                add(f"{bucket}_applied_le_credits", pyo.Constraint(
                    expr=sum(used[m] for m in cycles) <= carry_in + sum(credits[m] for m in cycles)
                ))
            else:
                add(f"{bucket}_applied_le_credits", pyo.Constraint(
                    cycles,
                    rule=lambda _m, m, u=used, cr=credits, ci=carry_in: (
                        sum(u[k] for k in range(m + 1)) <= ci + sum(cr[k] for k in range(m + 1))
                    ),
                ))
            applied[bucket] = used

        subtotal = add("subtotal", pyo.Expression(cycles, rule=lambda _m, m: (
            charges["generation"][m] - applied["generation"][m]
            + charges["delivery"][m] - applied["delivery"][m]
            + charges["nbc"][m] + self._bsc[m]
        )))
        acc_credits = add("acc_plus_credits", pyo.Expression(
            cycles, rule=lambda _m, m: cycle_sum(self._acc_credit, exp, m)
        ))
        acc_carry_in = program.carry_in_acc_plus if program is not None else 0.0
        acc = add("acc_plus_applied", pyo.Var(cycles, domain=pyo.NonNegativeReals))
        add("acc_plus_le_subtotal", pyo.Constraint(cycles, rule=lambda _m, m: acc[m] <= subtotal[m]))
        add("acc_plus_le_credits", pyo.Constraint(cycles, rule=lambda _m, m: (
            sum(acc[k] for k in range(m + 1)) <= acc_carry_in + sum(acc_credits[k] for k in range(m + 1))
        )))

        cost = sum(subtotal[m] - acc[m] for m in cycles)
        if self._nsc:
            surplus = add("net_surplus_kwh", pyo.Var(domain=pyo.NonNegativeReals))
            periods = premise.time_grid.periods
            add("net_surplus_le_exports_minus_imports", pyo.Constraint(
                expr=surplus <= self._dt * sum(exp[t] - imp[t] for t in periods)
            ))
            cost = cost - program.nsc_rate_per_kwh * surplus
        self._cost = cost

    def total_cost_expr(self, model: pyo.ConcreteModel, premise: Premise):
        if self._cost is None:
            raise RuntimeError("NBTBill.apply() must run before total_cost_expr()")
        return self._cost

    def evaluate_numeric(self, grid_import: np.ndarray, grid_export: np.ndarray, time_grid: TimeGrid) -> float:
        """The bill engine's net cost -- the same number the objective optimizes."""
        bill = compute_bill(
            time_grid.timestamps(),
            np.asarray(grid_import) * time_grid.dt_hours,
            np.asarray(grid_export) * time_grid.dt_hours,
            self.tariff,
            self.program,
            **self.bill_kwargs,
        )
        return bill.summary["net_cost"]
