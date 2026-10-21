"""Spike detection shared by the hub, the live filter and history rebuilds.

These rules used to be copied between the hub and the filter, and the copies
drifted apart: the hub accepted gigajoule meters that the filter then ignored,
so their clean sensors never moved. Everything that decides "is this energy
real?" lives here, with no Home Assistant I/O, so it can be tested directly.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
)
from homeassistant.core import State
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import MIN_ELAPSED_SECONDS, MIN_SPIKE_KWH

ENERGY_UNITS: frozenset[str] = frozenset(EnergyConverter.VALID_UNITS)


def to_kwh(value: float, unit: str) -> float:
    """Convert an amount of energy in `unit` to kWh."""
    return EnergyConverter.convert(value, unit, UnitOfEnergy.KILO_WATT_HOUR)


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert an amount of energy between two units."""
    return EnergyConverter.convert(value, from_unit, to_unit)


def is_energy_sensor(state: State | None) -> bool:
    """Whether a state belongs to a cumulative energy meter we can filter."""
    return (
        state is not None
        and state.attributes.get("state_class") == SensorStateClass.TOTAL_INCREASING
        and state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) in ENERGY_UNITS
    )


def reading(state: State | None) -> tuple[float, str] | None:
    """Return the numeric value and unit of an energy state, if it has one."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit not in ENERGY_UNITS:
        return None
    try:
        value = float(state.state)
    except TypeError, ValueError:
        return None
    # float() parses "nan" and "inf", and one NaN would poison every later
    # comparison and leave the running total NaN for good.
    if not math.isfinite(value):
        return None
    return value, unit


def implied_kw(jump_kwh: float, elapsed_seconds: float) -> float:
    """Average power that `jump_kwh` implies over the time available for it."""
    return jump_kwh / (max(elapsed_seconds, MIN_ELAPSED_SECONDS) / 3600.0)


def is_spike(jump_kwh: float, elapsed_seconds: float, max_power_kw: float) -> bool:
    """Whether an increase is physically impossible for the meter.

    Both must hold: the jump implies more than `max_power_kw` over the time
    the meter had to accumulate it, and it is large enough to matter. See
    MIN_SPIKE_KWH for why the size floor exists.
    """
    return (
        jump_kwh >= MIN_SPIKE_KWH
        and implied_kw(jump_kwh, elapsed_seconds) > max_power_kw
    )


def confirmed_at(
    old_state: State | None, value: float, unit: str, fallback: datetime
) -> datetime:
    """Return when the meter last vouched for the reading `value`.

    The rate test divides a jump by the time the meter had to accumulate it.
    Measuring that from when the previous reading first *appeared* makes an
    idle device look as though it had hours to build up whatever it reports
    next: a lamp idle for 14 hours could jump 655 kWh and imply under 50 kW.
    Most meters keep re-reporting an unchanged value, which Home Assistant
    records as `last_reported`, and that bounds the window properly.

    It is only trusted for a genuine re-report (`last_reported` after
    `last_changed`). A state restored at startup has the two equal, and
    trusting it would ignore the downtime during which a meter really can
    bank energy, flagging that banked energy as a spike.
    """
    if reading(old_state) != (value, unit):
        return fallback
    assert old_state is not None  # reading() returned a value
    if old_state.last_reported <= old_state.last_changed:
        return fallback
    return max(fallback, old_state.last_reported)


@dataclass(slots=True)
class CleanHistory:
    """A clean hourly series derived from a source meter's statistics."""

    # Hourly rows whose sums run from 0 up to `total`.
    rows: list[StatisticData]
    # Clean energy accumulated across the series.
    total: float
    # The clean running total at the end of the series.
    native: float
    # The source's own reading at the end of the series.
    source_state: float
    # End of the last hour in the series.
    end: datetime
    # Energy dropped as spikes, and the hours it was dropped from.
    suppressed: float = 0.0
    spike_hours: list[datetime] = field(default_factory=list)


def derive_clean_history(
    rows: Iterable[Mapping[str, Any]], unit: str, max_power_kw: float
) -> CleanHistory | None:
    """Rebuild a clean hourly series from a source's hourly statistics.

    Works from the source's `sum`, not its `state`. The recorder's sum is
    already reset-aware, so a genuine meter reset doesn't read as negative
    consumption; and it captures a spike that came and went inside one hour,
    which the hourly `state` would hide. The rate test then drops the hours
    such spikes landed in. A whole hour is dropped, so up to an hour of real
    consumption is lost alongside each spike: the same trade the live filter
    makes per reading.

    A falling sum means the source's statistics were adjusted by hand, and
    counts as no consumption rather than negative consumption.
    """
    usable: list[tuple[datetime, float, float]] = []
    for row in rows:
        if row.get("sum") is None or row.get("state") is None:
            continue
        start = row["start"]
        if not isinstance(start, datetime):
            start = dt_util.utc_from_timestamp(float(start))
        usable.append((start, float(row["sum"]), float(row["state"])))
    if not usable:
        return None

    first_start, _, native = usable[0]
    total = suppressed = 0.0
    spike_hours: list[datetime] = []
    out: list[StatisticData] = [{"start": first_start, "state": native, "sum": 0.0}]
    for (prev_start, prev_sum, _), (start, sum_, _) in pairwise(usable):
        delta = max(0.0, sum_ - prev_sum)
        elapsed = (start - prev_start).total_seconds()
        if delta and is_spike(to_kwh(delta, unit), elapsed, max_power_kw):
            suppressed += delta
            spike_hours.append(start)
            delta = 0.0
        total += delta
        native += delta
        out.append({"start": start, "state": native, "sum": total})

    last_start, _, source_state = usable[-1]
    return CleanHistory(
        rows=out,
        total=total,
        native=native,
        source_state=source_state,
        end=last_start + timedelta(hours=1),
        suppressed=suppressed,
        spike_hours=spike_hours,
    )


def continue_history(
    history: CleanHistory,
    value: float,
    now: datetime,
    unit: str,
    max_power_kw: float,
) -> tuple[float, float]:
    """Carry a derived history up to the source's current reading.

    Returns the running total to resume live filtering from, and any energy
    dropped as a spike since the last recorded hour.
    """
    increase = value - history.source_state
    if increase <= 0:
        # The meter fell since the last recorded hour (a spike reverting, or
        # a reset). There's nothing to count; live filtering re-anchors on it.
        return history.native, 0.0
    elapsed = (now - history.end).total_seconds()
    if is_spike(to_kwh(increase, unit), elapsed, max_power_kw):
        return history.native, increase
    return history.native + increase, 0.0
