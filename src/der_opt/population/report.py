"""What a population resolved to (docs/POPULATION_MODEL_SPEC.md sections 3 and 4a):
- the premises table: one row per premise, with its technology mix;
- the parameter table: every setting of every premise and device, with its value and
  source;
- the per-analysis checks `validate` runs before anything is solved.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from der_opt.population.resolved import ROLE_SETTINGS, ResolvedPremise

BILL_STREAMS = {"bill", "nbt_bill", "nem2_bill"}
# the device kinds each counterfactual policy works on
POLICY_KINDS = {"self_consumption": {"battery", "ev"}, "rule_based_ev": {"ev"}, "setpoint": {"thermostat"},
                "no_flexibility": {"solar", "battery", "ev", "thermostat", "flexible_load"}}


def premises_table(resolved: list[ResolvedPremise], built: list | None = None) -> pd.DataFrame:
    """One row per premise: where it is, its settings and their sources, and its devices
    by type (sizes summed when it has more than one of a type)."""
    built_by = {p.name: p for p in built or []}
    rows = []
    for r in resolved:
        v11_sizes = ("pv_kw_ac", "battery_kwh", "battery_kw", "ev_battery_kwh", "ev_charger_kw")  # the devices say it
        row = {"premise_id": r.premise_id, "origin": r.origin, "element_id": r.element_id,
               "customer_class": r.settings.get("customer_class", ""),
               **{k: v for k, v in r.extra.items() if k not in v11_sizes}}
        for setting in ROLE_SETTINGS:
            if r.settings.get(setting) is not None:
                row[setting] = r.settings[setting]
        sources = [f"{s}: {r.sources[s]}" for s in ROLE_SETTINGS if r.sources.get(s, "built-in") != "built-in"]
        row["settings_from"] = "; ".join(sources)
        premise = built_by.get(r.premise_id)
        devices = {d.name: d for d in premise.devices} if premise else {}
        for device in r.devices:
            kind = device.kind
            row[f"n_{kind}"] = row.get(f"n_{kind}", 0) + 1
            p = device.params
            built_device = devices.get(device.device_id)
            if kind == "solar":
                peak = float(np.max(built_device.available_kw)) if built_device is not None else np.nan
                row["solar_kw"] = round(row.get("solar_kw", 0.0) + peak, 2)
            elif kind in ("battery", "ev"):
                prefix = "battery" if kind == "battery" else "ev"
                row[f"{prefix}_kwh"] = row.get(f"{prefix}_kwh", 0.0) + float(p.get("capacity_kwh", np.nan))
                kw = max(float(p.get("p_max_charge_kw", 0) or 0), float(p.get("p_max_discharge_kw", 0) or 0))
                row["battery_kw" if kind == "battery" else "ev_charger_kw"] = row.get(
                    "battery_kw" if kind == "battery" else "ev_charger_kw", 0.0) + kw
                if kind == "ev":
                    row["ev_v2x"] = bool(row.get("ev_v2x")) or float(p.get("p_max_discharge_kw", 0) or 0) > 0
            elif kind == "thermostat":
                row["thermostat_kw"] = row.get("thermostat_kw", 0.0) + float(p.get("p_max_kw", np.nan))
            elif kind == "flexible_load":
                row["flexible_kw"] = row.get("flexible_kw", 0.0) + float(p.get("block_size_kw", 0)) * int(p.get("max_blocks", 1))
            if device.costs:
                for cost, value in device.costs.items():
                    row[cost] = row.get(cost, 0.0) + value
        rows.append(row)
    table = pd.DataFrame(rows)
    counts = [c for c in table.columns if c.startswith("n_")]
    table[counts] = table[counts].fillna(0).astype(int)
    return table


def parameters_table(resolved: list[ResolvedPremise]) -> pd.DataFrame:
    """One row per premise, device and setting: the value used and where it came from."""
    return pd.DataFrame([row for r in resolved for row in r.parameter_rows()],
                        columns=["premise_id", "device_id", "setting", "value", "source"])


def population_problems(resolved: list[ResolvedPremise]) -> list[str]:
    """What the premises' own analyses need and don't have (section 4a): a bill
    objective needs billing; a tariff with a baseline credit needs a territory."""
    from der_opt.billing import load_retail_tariff

    problems = []
    tariffs: dict = {}
    for r in resolved:
        for device in r.devices:
            policy = (device.counterfactual or {}).get("type")
            if policy is not None and device.kind not in POLICY_KINDS.get(policy, set()):
                fits = sorted(p for p, kinds in POLICY_KINDS.items() if device.kind in kinds)
                problems.append(f"{r.premise_id}/{device.device_id}: a {device.kind} can't take counterfactual "
                                f"{policy!r} (from {device.counterfactual_source}); it takes {fits}")
        streams = {s.get("type") for s in r.settings.get("value_streams") or []}
        billing = r.settings.get("billing")
        if streams & BILL_STREAMS and not billing:
            problems.append(f"{r.premise_id}: its objective is the bill, but it has no billing (a tariff)")
            continue
        if billing and billing.get("tariff"):
            path = billing["tariff"]
            if path not in tariffs:
                try:
                    tariffs[path] = load_retail_tariff(path)
                except (OSError, ValueError, KeyError) as exc:
                    tariffs[path] = exc
            tariff = tariffs[path]
            if isinstance(tariff, Exception):
                problems.append(f"{r.premise_id}: its tariff {path} doesn't load ({tariff})")
            elif tariff.baseline is not None and not billing.get("baseline_territory"):
                problems.append(f"{r.premise_id}: {tariff.name} has a baseline credit; give its baseline_territory")
    return problems
