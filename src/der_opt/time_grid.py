from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class TimeGrid:
    """A time horizon: fixed resolution, fixed number of steps, optional wall-clock start.

    Deliberately just a window (no hidden global clock), so the same object works for a
    standalone solve or as one window of a rolling-horizon sequence.
    """

    resolution_minutes: int
    horizon_steps: int
    start: datetime | None = None

    def __post_init__(self) -> None:
        if self.resolution_minutes <= 0:
            raise ValueError("resolution_minutes must be positive")
        if self.horizon_steps <= 0:
            raise ValueError("horizon_steps must be positive")

    @property
    def dt_hours(self) -> float:
        return self.resolution_minutes / 60.0

    @property
    def periods(self) -> range:
        return range(self.horizon_steps)

    def timestamps(self) -> list[datetime]:
        if self.start is None:
            raise ValueError("TimeGrid.start is not set; cannot produce timestamps")
        step = timedelta(minutes=self.resolution_minutes)
        return [self.start + i * step for i in self.periods]
