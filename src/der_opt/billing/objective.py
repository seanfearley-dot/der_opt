"""Utility bills as optimization objectives: NBTBill (Net Billing Tariff) and NEM2Bill
(NEM 2.0; see its docstring). Each one's cost equals der_opt.billing.compute_bill's net
cost for the same dispatch -- the bill engine is the reference; evaluate_numeric calls it.

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

from der_opt.billing.engine import NBTProgram, NEM2Program, _assign_cycles, compute_bill
from der_opt.billing.tariff import RetailTariff
from der_opt.premise.objective import ValueStream
from der_opt.time_grid import TimeGrid

if TYPE_CHECKING:
    from der_opt.premise.premise import Premise


class _BillObjective(ValueStream):
    """Shared by the bill objectives: billing cycles, the daily charge, and scoring a
    dispatch with the bill engine. Arguments match der_opt.billing.compute_bill."""

    def __init__(
        self,
        tariff: RetailTariff,
        program,
        time_grid: TimeGrid,
        income_tier: int = 3,
        cycle_start_dates=None,
        relevant_period_cycles: int = 12,
        baseline_territory: str | None = None,
        baseline_code: str = "basic",
    ):
        if time_grid.start is None:
            raise ValueError(f"{type(self).__name__} needs TimeGrid.start (real dates set the season, cycle, and day type)")
        if tariff.baseline is not None and baseline_territory is None:
            raise ValueError(f"{tariff.name} has a baseline credit: pass baseline_territory (e.g. 'X')")
        self.tariff = tariff
        self.program = program
        self.bill_kwargs = {
            "income_tier": income_tier,
            "cycle_start_dates": cycle_start_dates,
            "relevant_period_cycles": relevant_period_cycles,
            "baseline_territory": baseline_territory,
            "baseline_code": baseline_code,
        }
        self._index = pd.DatetimeIndex(time_grid.timestamps())
        self._dt = time_grid.dt_hours

        cycle_of = _assign_cycles(self._index, cycle_start_dates).to_numpy()
        labels = sorted(set(cycle_of))
        self._cycle_periods = [np.flatnonzero(cycle_of == label).tolist() for label in labels]
        self._is_true_up = len(labels) == relevant_period_cycles

        tier_key = f"tier_{income_tier}"
        if tariff.base_services_charge_per_day and tier_key not in tariff.base_services_charge_per_day:
            raise ValueError(f"tariff has no base services charge for income tier {income_tier}")
        bsc_per_day = tariff.base_services_charge_per_day.get(tier_key, 0.0)
        dates = self._index.normalize()
        self._cycle_dates = [dates[periods].unique() for periods in self._cycle_periods]
        self._bsc = [bsc_per_day * len(cycle_dates) for cycle_dates in self._cycle_dates]
        self._cost = None

    def total_cost_expr(self, model: pyo.ConcreteModel, premise: Premise):
        if self._cost is None:
            raise RuntimeError(f"{type(self).__name__}.apply() must run before total_cost_expr()")
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


class NBTBill(_BillObjective):
    """Cost = the customer's NBT bill (net of true-up and NSC) for the dispatch's import
    and export. Replaces EnergyArbitrage; don't use both, or energy is counted twice.
    program=None bills imports only."""

    def __init__(self, tariff: RetailTariff, program: NBTProgram | None, time_grid: TimeGrid, **bill_kwargs):
        super().__init__(tariff, program, time_grid, **bill_kwargs)
        if tariff.baseline is not None:
            raise NotImplementedError("a baseline-credit tariff under NBT isn't supported (NBT bills on E-ELEC)")
        index, dt = self._index, self._dt

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

        self._true_up = program is not None and self._is_true_up
        self._nsc = (
            self._true_up
            and program.nsc_rate_per_kwh is not None
            and program.nsc_debit_rate_per_kwh is not None
        )

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

    def pass_through_gain(self, premise: Premise) -> np.ndarray:
        """Importing and exporting x at once adds x times each bucket's charge and its
        credit. While each bucket's credit is all used, that pays where the credits
        (with ACC Plus) beat the charges (with non-bypassable charges, which credit
        never offsets). A bucket with credit to spare offsets its extra charge for free,
        so the other bucket's credit alone can make it pay. Generation credit outruns
        generation charges in far more hours (a solar home often has generation credit
        left), so this also counts hours where delivery credit alone pays; a home with
        delivery credit to spare instead is caught by the solve's check."""
        common = self._acc_credit - self._charge["nbc"]
        all_used = (self._credit["generation"] - self._charge["generation"]
                    + self._credit["delivery"] - self._charge["delivery"])
        generation_spare = self._credit["delivery"] - self._charge["delivery"]
        return common + np.maximum(all_used, generation_spare)


class NEM2Bill(_BillObjective):
    """Cost = the customer's NEM 2.0 bill for the dispatch's import and export (see
    der_opt.billing.engine._compute_nem2_bill for the rules). Replaces EnergyArbitrage.

    Formulation:
      - energy: within a TOU period, imports and exports are billed and credited at the
        same rate, so the cycle's energy charge is linear in (import - export) per interval
      - non-bypassable charges on each interval's imports net of its exports: a_t >= 0,
        a_t >= import_t - export_t
      - energy settles at the true-up as max(0, accrued energy charges): E >= 0, E >= sum
      - NSC (true-up only): s >= 0, s <= exports - imports for the year, credited
      - baseline credit (if the tariff has one): -credit * clip(net kWh, -allowance,
        allowance) per cycle. It lowers a net consumer's charges and a net generator's
        credit, so its value isn't convex in net kWh: each cycle gets two yes/no
        variables (net consumer or not; net generation above the allowance or not),
        which makes this a MILP and needs both meter limits (poi_limit_kw, or
        import_limit_kw and export_limit_kw) for the big-M bounds.

    The objective counts leftover credit at the end as $0, which under NEM2 is literally
    true at a true-up (it's zeroed)."""

    def __init__(self, tariff: RetailTariff, program: NEM2Program, time_grid: TimeGrid, **bill_kwargs):
        super().__init__(tariff, program, time_grid, **bill_kwargs)
        rates = tariff.bucket_rates(self._index)
        self._energy_rate = (rates["generation"] + rates["delivery"]).to_numpy() * self._dt
        self._nbc_rate = rates["nbc"].to_numpy() * self._dt
        self._nsc = self._is_true_up and program.nsc_rate_per_kwh is not None
        if tariff.baseline is not None:
            territory, code = bill_kwargs["baseline_territory"], bill_kwargs.get("baseline_code", "basic")
            self._allowance = [
                tariff.baseline.allowance_kwh(dates, tariff.season_by_month, territory, code)
                for dates in self._cycle_dates
            ]

    def pass_through_gain(self, premise: Premise) -> np.ndarray:
        return np.zeros(premise.time_grid.horizon_steps)  # every term sees import - export

    def apply(self, model: pyo.ConcreteModel, premise: Premise) -> None:
        imp, exp = premise.grid_import, premise.grid_export
        periods = premise.time_grid.periods
        cycles = range(len(self._cycle_periods))
        name = f"{premise.name}_nem2_bill"

        def add(suffix, component):
            model.add_component(f"{name}_{suffix}", component)
            return component

        nbc_kw = add("nbc_kw", pyo.Var(periods, domain=pyo.NonNegativeReals))
        add("nbc_kw_ge_net_import", pyo.Constraint(periods, rule=lambda _m, t: nbc_kw[t] >= imp[t] - exp[t]))
        nbc = sum(self._nbc_rate[t] * nbc_kw[t] for t in periods)

        energy = add("energy_due", pyo.Expression(cycles, rule=lambda _m, m: sum(
            self._energy_rate[t] * (imp[t] - exp[t]) for t in self._cycle_periods[m]
        )))
        accrued = sum(energy[m] for m in cycles)

        if self.tariff.baseline is not None:
            if premise.import_limit_kw is None or premise.export_limit_kw is None:
                raise ValueError("NEM2Bill with a baseline credit needs both meter limits (poi_limit_kw, or "
                                 "import_limit_kw and export_limit_kw) for its big-M bounds")
            credit = self.tariff.baseline.credit_per_kwh
            # a cycle's net kWh is bounded by the larger limit in either direction
            limit = max(premise.import_limit_kw, premise.export_limit_kw)
            big_m = [limit * self._dt * len(self._cycle_periods[m]) for m in cycles]
            allowance = self._allowance
            net_in = add("net_consumption_kwh", pyo.Var(cycles, domain=pyo.NonNegativeReals))
            net_out = add("net_generation_kwh", pyo.Var(cycles, domain=pyo.NonNegativeReals))
            consumer = add("is_net_consumer", pyo.Var(cycles, domain=pyo.Binary))
            above = add("is_generation_above_baseline", pyo.Var(cycles, domain=pyo.Binary))
            baseline_in = add("baseline_consumption_kwh", pyo.Var(cycles, domain=pyo.NonNegativeReals))
            baseline_out = add("baseline_generation_kwh", pyo.Var(cycles, domain=pyo.NonNegativeReals))
            add("net_split", pyo.Constraint(cycles, rule=lambda _m, m: net_in[m] - net_out[m] == self._dt * sum(
                imp[t] - exp[t] for t in self._cycle_periods[m]
            )))
            add("net_in_only_if_consumer", pyo.Constraint(cycles, rule=lambda _m, m: net_in[m] <= big_m[m] * consumer[m]))
            add("net_out_only_if_generator", pyo.Constraint(
                cycles, rule=lambda _m, m: net_out[m] <= big_m[m] * (1 - consumer[m])
            ))
            # baseline_in = min(net_in, allowance): the credit rewards it, so upper bounds suffice
            add("baseline_in_le_net", pyo.Constraint(cycles, rule=lambda _m, m: baseline_in[m] <= net_in[m]))
            add("baseline_in_le_allowance", pyo.Constraint(cycles, rule=lambda _m, m: baseline_in[m] <= allowance[m]))
            # baseline_out = min(net_out, allowance): it costs, so it needs lower bounds,
            # one of which is switched off by `above`
            add("baseline_out_ge_net", pyo.Constraint(
                cycles, rule=lambda _m, m: baseline_out[m] >= net_out[m] - big_m[m] * above[m]
            ))
            add("baseline_out_ge_allowance", pyo.Constraint(
                cycles, rule=lambda _m, m: baseline_out[m] >= allowance[m] - big_m[m] * (1 - above[m])
            ))
            accrued = accrued + credit * sum(baseline_out[m] - baseline_in[m] for m in cycles)

        energy_owed = add("energy_owed", pyo.Var(domain=pyo.NonNegativeReals))
        add("energy_owed_ge_accrued", pyo.Constraint(expr=energy_owed >= accrued))

        cost = nbc + sum(self._bsc) + energy_owed
        if self._nsc:
            surplus = add("net_surplus_kwh", pyo.Var(domain=pyo.NonNegativeReals))
            add("net_surplus_le_exports_minus_imports", pyo.Constraint(
                expr=surplus <= self._dt * sum(exp[t] - imp[t] for t in periods)
            ))
            cost = cost - self.program.nsc_rate_per_kwh * surplus
        self._cost = cost
