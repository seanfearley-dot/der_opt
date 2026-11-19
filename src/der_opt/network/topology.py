"""A radial distribution network: transformers and line sections in a tree, with
premises attached to them.

Flow through an element is the sum of the net imports (import - export) of every
premise below it -- a power balance only. Losses, voltage, and reactive power aren't
modeled, so a kVA nameplate has to be converted to kW (times power factor) by the
caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
    """

    name: str
    rating_kw: float | None = None
    overload_cost_per_kwh: float | None = None
    parent: str | None = None


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
