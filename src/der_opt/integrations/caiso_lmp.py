"""CAISO locational marginal prices (LMP) as a price input.

The model takes any price as one $/kWh value per time step, on the scenario's local
clock (naive timestamps, the same clock the TOU tariffs use). Wholesale LMP arrives
differently on every count, and this module is the reference for the conversion:

  ISO data                              what a scenario needs
  $/MWh                                 $/kWh                        (divide by 1000)
  many pricing nodes                    one node                     (pick it)
  UTC, time-zone aware                  local clock, naive           (convert, then drop the zone)
  5-, 15- or 60-minute intervals        the run's resolution         (average down, or hold each
                                                                      price across finer steps)
  daylight-saving days of 23 or 25 h    24 h every day               (average the repeated hour in
                                                                      November; repeat the hour
                                                                      before the skipped one in March)

`lmp_price_table` does exactly that to any table of interval-start times, nodes and
$/MWh prices, whichever ISO or tool it came from. `fetch_caiso_lmp` downloads CAISO's
from its public OASIS API (no account or key). `der-opt fetch-lmp` runs both and
writes a CSV a scenario can use as a price:

    import_price: {type: csv, path: data/lmp.csv, column: lmp_per_kwh}
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

OASIS_URL = "https://oasis.caiso.com/oasisapi/SingleZip"
LOCAL_TZ = "America/Los_Angeles"


@dataclass(frozen=True)
class _Market:
    query: str
    run_id: str
    version: str
    price_column: str
    max_days_per_request: int  # OASIS rejects longer requests (error 1004)
    interval_minutes: int


MARKETS = {
    "DAM": _Market("PRC_LMP", "DAM", "12", "MW", 30, 60),  # day-ahead, hourly
    "RTPD": _Market("PRC_RTPD_LMP", "RTPD", "1", "PRC", 5, 15),  # real-time pre-dispatch, 15-min
    "RTM": _Market("PRC_INTVL_LMP", "RTM", "1", "MW", 1, 5),  # real-time dispatch, 5-min
}


def _oasis_time(day: date, tz: str) -> str:
    """Local midnight starting `day`, as OASIS's UTC timestamp format."""
    utc = pd.Timestamp(day).tz_localize(tz).tz_convert("UTC")
    return utc.strftime("%Y%m%dT%H:%M-0000")


def _read_oasis_zip(content: bytes) -> pd.DataFrame:
    archive = zipfile.ZipFile(io.BytesIO(content))
    name = archive.namelist()[0]
    if name.endswith(".csv"):
        return pd.read_csv(archive.open(name))
    # OASIS reports errors as an XML file in the zip, with an HTTP 200
    text = archive.open(name).read().decode("utf-8", errors="replace")
    details = " ".join(re.findall(r"<m:ERR_(?:CODE|DESC)>([^<]*)<", text)) or text[:300]
    raise RuntimeError(f"CAISO OASIS returned an error: {details}")


def fetch_caiso_lmp(
    node: str,
    start: date,
    end: date,
    market: str = "DAM",
    tz: str = LOCAL_TZ,
    pause_s: float = 5.0,
    session=None,
) -> pd.DataFrame:
    """Raw LMP for one CAISO pricing node (e.g. "TH_NP15_GEN-APND", the NP15 trading
    hub) from local midnight starting `start` up to local midnight starting `end`
    (exclusive). Returns interval_start_utc, node, lmp_per_mwh -- unconverted; pass it
    to lmp_price_table. Long ranges go out as several requests, `pause_s` apart (OASIS
    rate-limits clients that ask faster than about one request per 5 seconds)."""
    if market not in MARKETS:
        raise ValueError(f"market must be one of {sorted(MARKETS)}, got {market!r}")
    if end <= start:
        raise ValueError("end must be after start")
    spec = MARKETS[market]
    if session is None:
        import requests

        session = requests

    frames = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=spec.max_days_per_request), end)
        if frames and pause_s:
            time.sleep(pause_s)
        response = session.get(OASIS_URL, params={
            "queryname": spec.query, "market_run_id": spec.run_id, "version": spec.version, "node": node,
            "startdatetime": _oasis_time(chunk_start, tz), "enddatetime": _oasis_time(chunk_end, tz),
            "resultformat": "6",
        }, timeout=120)
        response.raise_for_status()
        raw = _read_oasis_zip(response.content)
        # one row per component (energy, congestion, losses, GHG); LMP is their total
        raw = raw[raw["LMP_TYPE"] == "LMP"]
        frames.append(pd.DataFrame({
            "interval_start_utc": pd.to_datetime(raw["INTERVALSTARTTIME_GMT"], utc=True),
            "node": raw["NODE"],
            "lmp_per_mwh": raw[spec.price_column].astype(float),
        }))
        chunk_start = chunk_end
    return pd.concat(frames, ignore_index=True).sort_values("interval_start_utc").reset_index(drop=True)


def lmp_price_table(
    frame: pd.DataFrame,
    resolution_minutes: int = 60,
    node: str | None = None,
    tz: str = LOCAL_TZ,
    time_column: str = "interval_start_utc",
    node_column: str = "node",
    price_column: str = "lmp_per_mwh",
) -> pd.DataFrame:
    """Converts LMP rows (interval start, node, $/MWh) into what a scenario reads: a
    `timestamp` index on the local clock, one row per `resolution_minutes`, and an
    `lmp_per_kwh` column. Times may be time-zone aware or naive UTC. Works for any
    ISO's data given the right column names (e.g. gridstatus output: time_column=
    "Interval Start", node_column="Location", price_column="LMP")."""
    rows = frame
    if node_column in frame.columns:
        nodes = frame[node_column].unique()
        if node is None and len(nodes) > 1:
            raise ValueError(f"the data has {len(nodes)} pricing nodes; pick one with node= (e.g. {nodes[0]!r})")
        if node is not None:
            rows = frame[frame[node_column] == node]
            if rows.empty:
                raise ValueError(f"node {node!r} isn't in the data")

    times = pd.to_datetime(rows[time_column], utc=True)
    per_kwh = pd.Series(rows[price_column].astype(float).to_numpy() / 1000.0, index=pd.DatetimeIndex(times))
    per_kwh = per_kwh.groupby(level=0).mean().sort_index()

    step = f"{resolution_minutes}min"
    local = per_kwh.tz_convert(tz)
    # average finer intervals into each step, in real (zone-aware) time
    local = local.resample(step).mean().dropna()
    # on the local clock, November's repeated hour appears twice: average it
    clock = local.tz_localize(None).groupby(level=0).mean()
    # March's skipped hour, and steps finer than the data, take the price before them
    regular = pd.date_range(clock.index[0], clock.index[-1], freq=step)
    clock = clock.reindex(regular).ffill()
    return clock.rename("lmp_per_kwh").rename_axis("timestamp").to_frame()


def write_lmp_csv(table: pd.DataFrame, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(path, float_format="%.6f")
    return path
