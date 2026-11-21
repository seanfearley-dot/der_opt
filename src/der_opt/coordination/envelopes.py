"""Dynamic operating envelopes (docs/COORDINATION_SPEC.md section 4).

Four stages, each with interchangeable methods:
  1. limits    every network limit as a weighted sum: sum(coefficient x participant's
               flow) <= limit, by quantity and step. Thermal limits are the only kind
               so far: coefficient 1 for each participant below an element.
  2. allocation  a method turns the limits into each participant's envelope, given
               per-participant inputs (needs, service limits, weights).
  3. nesting   min_along_path or top_down, on a feeder with more than one level.
  4. granularity  a new envelope each step, per block of hours, or one for the run.

Every proportional method is a (weight, cap) pair per participant: shares are split in
proportion to the weights, and what a capped participant can't take goes to the rest
(water-filling). equal is weight 1; pro_rata_need, weight = need; max_min, weight 1
capped at need; service_size, weight = service limit; weights, the given weight.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from der_opt.network.network_model import FixedLoads, fixed_below
from der_opt.network.topology import Feeder

QUANTITIES = ("import_kw", "export_kw")


@dataclass
class Limit:
    """sum(coefficients[p] x flow_p[t]) <= bound[t], for one quantity."""

    name: str
    quantity: str
    coefficients: dict[str, float]
    bound: np.ndarray


def thermal_limits(feeder: Feeder, participants: list[str], fixed: FixedLoads, steps: int,
                   margin_pct: float = 0.0) -> list[Limit]:
    """One import and one export limit per rated element: the rating, less the margin,
    less (for import) or plus (for export) the fixed flow through it."""
    limits = []
    for element in feeder.elements:
        if element.rating_kw is None:
            continue
        below = [p for p in feeder.premises_below(element.name) if p in participants]
        if not below:
            continue
        rating = element.rating_kw * (1 - margin_pct / 100)
        flow = fixed_below(feeder, element.name, fixed)
        flow = np.zeros(steps) if flow is None else flow
        coefficients = dict.fromkeys(below, 1.0)
        limits.append(Limit(element.name, "import_kw", coefficients, np.clip(rating - flow, 0, None)))
        limits.append(Limit(element.name, "export_kw", coefficients, np.clip(rating + flow, 0, None)))
    return limits


def water_fill(total: float, weights: np.ndarray, caps: np.ndarray) -> np.ndarray:
    """Split `total` in proportion to `weights`, never giving anyone more than its cap;
    what a capped one can't take goes to the others."""
    out = np.zeros(len(weights))
    open_ = weights > 0
    left = float(total)
    while left > 1e-12 and open_.any():
        share = left * np.where(open_, weights, 0) / weights[open_].sum()
        over = open_ & (out + share >= caps - 1e-12)
        if not over.any():
            out += share
            break
        fill = np.where(over, caps - out, 0)
        out += fill
        left -= fill.sum()
        open_ &= ~over
    return out


@dataclass
class Inputs:
    """What allocation methods may use, per participant (arrays by step)."""

    participants: list[str]
    needs: dict[str, dict[str, np.ndarray]]  # quantity -> participant -> kW with no program
    service: dict[str, dict[str, float | None]]  # quantity -> participant -> limit
    weights: dict[str, float | None]


def _weights_caps(method, quantity: str, names: list[str], inputs: Inputs, t: int) -> tuple[np.ndarray, np.ndarray]:
    n = len(names)
    inf = np.full(n, np.inf)
    if method.method == "equal":
        return np.ones(n), inf
    if method.method == "pro_rata_need":
        return np.array([max(inputs.needs[quantity][p][t], 0.0) for p in names]), inf
    if method.method == "max_min":
        return np.ones(n), np.array([max(inputs.needs[quantity][p][t], 0.0) for p in names])
    if method.method == "service_size":
        values = [inputs.service[quantity][p] for p in names]
        if any(v is None for v in values):
            missing = [p for p, v in zip(names, values, strict=True) if v is None]
            raise ValueError(f"envelopes: service_size needs a {quantity.replace('_kw', '')} limit for {missing}")
        return np.array(values, dtype=float), inf
    if method.method == "weights":
        values = [inputs.weights[p] for p in names]
        if any(v is None for v in values):
            missing = [p for p, v in zip(names, values, strict=True) if v is None]
            raise ValueError(f"envelopes: method weights needs envelope_weight (or the column) for {missing}")
        return np.array(values, dtype=float), inf
    raise ValueError(f"envelopes: {method.method} isn't a proportional method")


def _min_along_path(limits: list[Limit], method, quantity: str, inputs: Inputs, steps: int) -> dict[str, np.ndarray]:
    out = {p: np.full(steps, np.inf) for p in inputs.participants}
    for limit in (lim for lim in limits if lim.quantity == quantity):
        names = list(limit.coefficients)
        for t in range(steps):
            weights, caps = _weights_caps(method, quantity, names, inputs, t)
            shares = water_fill(limit.bound[t], weights, caps)
            for p, share in zip(names, shares, strict=True):
                out[p][t] = min(out[p][t], share)
    return out


def _top_down(feeder: Feeder, limits: list[Limit], method, quantity: str, inputs: Inputs,
              steps: int) -> dict[str, np.ndarray]:
    bound = {lim.name: lim.bound for lim in limits if lim.quantity == quantity}
    participants = set(inputs.participants)
    children: dict[str | None, list] = {}
    for element in feeder.elements:
        children.setdefault(element.parent, []).append(("element", element.name))
    for premise, element in feeder.attachments.items():
        if premise in participants:
            children.setdefault(element, []).append(("premise", premise))
    out = {p: np.zeros(steps) for p in inputs.participants}

    def under(kind, name) -> list[str]:
        if kind == "premise":
            return [name]
        return [p for p in feeder.premises_below(name) if p in participants]

    def allocate(element: str, t: int, given: float) -> None:
        kids = [k for k in children.get(element, []) if under(*k)]
        if not kids:
            return
        if np.isinf(given):  # nothing above limits this element: each child decides
            for kind, name in kids:
                if kind == "premise":
                    out[name][t] = np.inf
                else:
                    allocate(name, t, bound[name][t] if name in bound else np.inf)
            return
        names = [p for k in kids for p in under(*k)]
        w, c = _weights_caps(method, quantity, names, inputs, t)
        weight = dict(zip(names, w, strict=True))
        cap = dict(zip(names, c, strict=True))
        kid_weights, kid_caps = [], []
        for kind, name in kids:
            members = under(kind, name)
            kid_weights.append(sum(weight[p] for p in members))
            own = bound[name][t] if kind == "element" and name in bound else np.inf
            kid_caps.append(min(own, sum(cap[p] for p in members)))
        if sum(kid_weights) == 0:
            kid_weights = [float(len(under(*k))) for k in kids]
        shares = water_fill(given, np.array(kid_weights, dtype=float), np.array(kid_caps, dtype=float))
        for (kind, name), share in zip(kids, shares, strict=True):
            if kind == "premise":
                out[name][t] = share
            else:
                allocate(name, t, share)

    for root in children.get(None, []):
        _, name = root
        for t in range(steps):
            members = under("element", name)
            if not members:
                continue
            allocate(name, t, bound[name][t] if name in bound else np.inf)
    return out


def _granular(values: np.ndarray, granularity, steps_per_hour: float) -> np.ndarray:
    if granularity == "step":
        return values
    if granularity == "static":
        return np.full(len(values), values.min())
    block = max(1, round(granularity["block_hours"] * steps_per_hour))
    out = values.copy()
    for start in range(0, len(values), block):
        out[start:start + block] = values[start:start + block].min()
    return out


def _from_file(path: str, participants: list[str], index: pd.DatetimeIndex, quantity: str) -> dict[str, np.ndarray]:
    frame = pd.read_csv(path, encoding="utf-8-sig")
    missing_cols = {"timestamp", "premise_id", quantity} - set(frame.columns)
    if missing_cols:
        raise ValueError(f"{path}: missing column(s) {sorted(missing_cols)}")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    table = frame.pivot_table(index="timestamp", columns="premise_id", values=quantity)
    out = {}
    for p in participants:
        if p not in table:
            raise ValueError(f"{path}: no {quantity} envelope for {p}")
        series = table[p].reindex(index)
        if series.isna().any():
            raise ValueError(f"{path}: {p}'s {quantity} envelope doesn't cover the run ({series.isna().sum()} steps missing)")
        out[p] = series.to_numpy(dtype=float)
    return out


def compute_envelopes(config, feeder: Feeder, inputs: Inputs, fixed: FixedLoads, index: pd.DatetimeIndex,
                      dt_hours: float) -> pd.DataFrame:
    """Each participant's import and export envelope by step: a frame of
    timestamp, premise_id, quantity, kw (inf: no envelope in that direction)."""
    steps = len(index)
    limits = thermal_limits(feeder, inputs.participants, fixed, steps, config.headroom.margin_pct)
    rows = []
    for quantity, method in (("import_kw", config.import_), ("export_kw", config.export)):
        if method is None:
            continue
        if method.method == "fixed":
            if method.kw is None:
                raise ValueError("envelopes: method fixed needs kw")
            shares = {p: np.full(steps, float(method.kw)) for p in inputs.participants}
        elif method.method == "file":
            if method.path is None:
                raise ValueError("envelopes: method file needs path")
            shares = _from_file(method.path, inputs.participants, index, quantity)
        elif config.nesting == "top_down":
            shares = _top_down(feeder, limits, method, quantity, inputs, steps)
        else:
            shares = _min_along_path(limits, method, quantity, inputs, steps)
        for p in inputs.participants:
            values = _granular(np.asarray(shares[p], dtype=float), config.granularity, 1 / dt_hours)
            service = inputs.service[quantity][p]
            if service is not None:
                values = np.minimum(values, service)
            if quantity == "import_kw":
                values = np.maximum(values, config.min_kw)
            rows.append(pd.DataFrame({"timestamp": index, "premise_id": p, "quantity": quantity, "kw": values}))
    if not rows:
        return pd.DataFrame(columns=["timestamp", "premise_id", "quantity", "kw"])
    return pd.concat(rows, ignore_index=True)


def envelope_arrays(frame: pd.DataFrame, premise: str, steps: int) -> tuple[np.ndarray | None, np.ndarray | None]:
    """(import caps, export caps) for one participant, or None where there's none."""
    out = []
    for quantity in QUANTITIES:
        rows = frame[(frame["premise_id"] == premise) & (frame["quantity"] == quantity)]
        out.append(rows["kw"].to_numpy(dtype=float) if len(rows) == steps else None)
    return out[0], out[1]
