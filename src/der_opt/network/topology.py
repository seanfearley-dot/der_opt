"""A radial distribution network: transformers and line sections in a tree, with
premises attached to them.

Flow through an element is the sum of the net imports (import - export) of every
premise below it -- a power balance only. Losses, voltage, and reactive power aren't
modeled, so a kVA nameplate has to be converted to kW (times power factor) by the
caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class NetworkElement:
    """A transformer or line section.

    rating_kw: thermal limit, applied in both directions (load and backfeed); None
        means unconstrained.
    overload_cost_per_kwh: None makes the rating a hard limit. A number makes it soft:
        flow may exceed the rating at this cost per kWh over, standing in for
        transformer loss of life. A hard limit can make a run infeasible when
        uncontrollable load alone exceeds the rating.
    parent: the element upstream (toward the substation); None for the root.
    emergency_rating_kw: an emergency rating above the normal one (coordination spec
        v1.1, section 1b): flow between the two costs overload_cost_per_kwh; above it,
        violation_cost_per_kwh, or a hard wall without one.
    emergency_kwh_per_day: at most this many kWh above normal each local day.
    """

    name: str
    rating_kw: float | None = None
    overload_cost_per_kwh: float | None = None
    parent: str | None = None
    emergency_rating_kw: float | None = None
    violation_cost_per_kwh: float | None = None
    emergency_kwh_per_day: float | None = None

    def __post_init__(self):
        where = f"network element {self.name}"
        if self.emergency_rating_kw is None:
            if self.violation_cost_per_kwh is not None or self.emergency_kwh_per_day is not None:
                raise ValueError(f"{where}: violation_cost_per_kwh and emergency_kwh_per_day need an emergency_rating_kw")
            return
        if self.rating_kw is None or self.emergency_rating_kw <= self.rating_kw:
            raise ValueError(f"{where}: emergency_rating_kw ({self.emergency_rating_kw:g}) must be above its rating_kw "
                             f"({self.rating_kw})")
        if self.overload_cost_per_kwh is None:
            raise ValueError(f"{where}: an emergency rating needs overload_cost_per_kwh, the cost of running between "
                             "normal and emergency")

    def bands(self, flow_kw: np.ndarray, dt: float, days: np.ndarray | None = None) -> tuple[float, float, np.ndarray]:
        """(overload cost, violation cost, kW above the hard limit) for flows (either
        direction): what a run's flows cost on this element's bands."""
        flow = np.abs(np.asarray(flow_kw, dtype=float))
        if self.rating_kw is None:
            return 0.0, 0.0, np.zeros(len(flow))
        if self.emergency_rating_kw is None:
            over = np.clip(flow - self.rating_kw, 0, None)
            if self.overload_cost_per_kwh is None:
                return 0.0, 0.0, over
            return self.overload_cost_per_kwh * float(over.sum()) * dt, 0.0, np.zeros(len(flow))
        band = np.clip(flow - self.rating_kw, 0, self.emergency_rating_kw - self.rating_kw) * dt
        beyond = np.clip(flow - self.emergency_rating_kw, 0, None)
        spill = 0.0  # emergency-band kWh past the daily budget: priced as violation
        if self.emergency_kwh_per_day is not None and days is not None:
            for day in np.unique(days):
                spill += max(0.0, float(band[days == day].sum()) - self.emergency_kwh_per_day)
        overload = self.overload_cost_per_kwh * (float(band.sum()) - spill)
        if self.violation_cost_per_kwh is None:
            return overload, 0.0, beyond
        return overload, self.violation_cost_per_kwh * (float(beyond.sum()) * dt + spill), np.zeros(len(flow))


@dataclass
class Feeder:
    name: str
    elements: list[NetworkElement]
    # premise name -> element it's served from
    attachments: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        names = [element.name for element in self.elements]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate network element names: {sorted(duplicates)}")
        self._by_name = {element.name: element for element in self.elements}
        for element in self.elements:
            if element.parent is not None and element.parent not in self._by_name:
                raise ValueError(f"{element.name}'s parent {element.parent!r} isn't a network element")
        for element in self.elements:  # a parent chain that never reaches a root is a cycle
            seen = set()
            node = element
            while node.parent is not None:
                if node.name in seen:
                    raise ValueError(f"network elements form a cycle through {node.name!r}")
                seen.add(node.name)
                node = self._by_name[node.parent]
        for premise, element in self.attachments.items():
            if element not in self._by_name:
                raise ValueError(f"premise {premise!r} is attached to unknown element {element!r}")

    def element(self, name: str) -> NetworkElement:
        return self._by_name[name]

    def path_to_root(self, element_name: str) -> list[str]:
        """The element and every element upstream of it."""
        path = [element_name]
        while (parent := self._by_name[path[-1]].parent) is not None:
            path.append(parent)
        return path

    def premises_below(self, element_name: str) -> list[str]:
        """Premises whose power flows through this element."""
        return [premise for premise, attached in self.attachments.items() if element_name in self.path_to_root(attached)]
