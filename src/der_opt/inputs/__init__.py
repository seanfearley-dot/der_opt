"""Input data in the approved spec format (docs/INPUT_DATA_SPEC.md): readers,
validation, and quality checks."""

from der_opt.inputs.quality import check_series
from der_opt.inputs.spec import (
    SPEC_VERSION,
    NetworkFile,
    PremisesFile,
    SpecError,
    TimeSeriesFile,
    check_file,
    detect_kind,
    read_network,
    read_premises,
    read_timeseries,
)

__all__ = [
    "SPEC_VERSION",
    "NetworkFile",
    "PremisesFile",
    "SpecError",
    "TimeSeriesFile",
    "check_file",
    "check_series",
    "detect_kind",
    "read_network",
    "read_premises",
    "read_timeseries",
]
