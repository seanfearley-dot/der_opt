from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Each device defines its own state keys (e.g. {"soc": ...} for a battery, {"temp": ...}
# for a thermostat); there's no shared schema across device types, so a device's state
# is just a plain dict rather than a per-device-type dataclass.
DeviceState = dict[str, Any]


@dataclass
class PremiseState:
    """All devices' states at one point in the simulation."""

    t: int
    device_states: dict[str, DeviceState] = field(default_factory=dict)
