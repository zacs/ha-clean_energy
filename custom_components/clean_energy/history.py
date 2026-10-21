"""Replace a clean sensor's statistics with ones derived from its source.

The recorder builds each hourly row from five-minute rows, and seeds every
five-minute compile from the latest five-minute row. That has two consequences
which the obvious approach (import hourly rows, carry on) gets wrong:

* With no five-minute rows the recorder starts counting from zero, so live
  statistics restart at 0 right where imported history ends, and every
  dashboard period spanning that moment reads wrong.
* Changing a running total makes the recorder count the change as
  consumption: a drop reads as a meter reset, and the whole new value is
  added on top.

So a rebuild runs in three steps:

1. If the running total must change, change it, then wait for the recorder
   to compile the five-minute window it changed in. That compile records
   the change as consumption, into statistics about to be discarded.
2. Clear the clean sensor's statistics and import the derived hours, with
   sums running from 0 up to the clean total. A series has to start near 0:
   the dashboard measures the first row's change against 0.
3. The next compile finds no five-minute rows and starts from 0 at the
   current value. Once it has run, shift that window and everything after it
   up by the imported total, so live statistics continue where history ends.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import (
    EVENT_RECORDER_5MIN_STATISTICS_GENERATED,
    get_instance,
)
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import COMPILE_WAIT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

_FIVE_MINUTES = timedelta(minutes=5)

# The recorder queues each five-minute compile ten seconds after the window
# closes. A clear queued just before that would let the closing window's
# compile run against cleared statistics, zero-pointing a window that may sit
# in the last imported hour, which step 3's hourly shift would then double.
# Staying clear of the first half-minute after each boundary avoids that.
_BOUNDARY_MARGIN = timedelta(seconds=30)


def five_minute_floor(moment: datetime) -> datetime:
    """Return the start of the five-minute window containing `moment`."""
    return moment.replace(
        minute=moment.minute - moment.minute % 5, second=0, microsecond=0
    )


async def async_source_hours(
    hass: HomeAssistant, statistic_id: str, unit: str
) -> list[dict[str, Any]]:
    """Return a statistic's complete hourly history, in `unit`."""
    end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)

    def _fetch() -> list[dict[str, Any]]:
        rows = statistics_during_period(
            hass,
            dt_util.utc_from_timestamp(0),
            end,
            {statistic_id},
            "hour",
            {"energy": unit},
            {"sum", "state"},
        )
        return list(rows.get(statistic_id, []))

    return await get_instance(hass).async_add_executor_job(_fetch)


async def async_has_hours_before(
    hass: HomeAssistant, statistic_id: str, moment: datetime
) -> bool:
    """Whether a statistic has hourly rows starting before `moment`."""

    def _fetch() -> bool:
        rows = statistics_during_period(
            hass,
            dt_util.utc_from_timestamp(0),
            moment,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        return bool(rows.get(statistic_id))

    return await get_instance(hass).async_add_executor_job(_fetch)


async def _async_has_five_minute_row_from(
    hass: HomeAssistant, statistic_id: str, moment: datetime
) -> bool:
    def _fetch() -> bool:
        rows = statistics_during_period(
            hass, moment, None, {statistic_id}, "5minute", None, {"sum"}
        )
        return bool(rows.get(statistic_id))

    return await get_instance(hass).async_add_executor_job(_fetch)


async def async_wait_until(hass: HomeAssistant, moment: datetime) -> None:
    """Sleep until `moment`, on Home Assistant's clock."""
    if dt_util.utcnow() >= moment:
        return
    done: asyncio.Future[None] = hass.loop.create_future()

    @callback
    def _wake(_now: datetime) -> None:
        if not done.done():
            done.set_result(None)

    unsub = async_track_point_in_utc_time(hass, _wake, moment)
    try:
        await done
    finally:
        unsub()


async def async_wait_out_compile_boundary(hass: HomeAssistant) -> None:
    """Wait until a clear can't race the compile of the window just closed."""
    now = dt_util.utcnow()
    window = five_minute_floor(now)
    if now - window < _BOUNDARY_MARGIN:
        await async_wait_until(hass, window + _BOUNDARY_MARGIN)


async def async_wait_for_compile(
    hass: HomeAssistant, statistic_id: str, window: datetime
) -> bool:
    """Wait until the recorder has compiled `statistic_id` for `window`.

    Waits for a five-minute row starting at or after `window` to exist,
    rather than trusting event timing: a backed-up recorder can deliver one
    window's completion event when the next window was expected.
    """
    ready = asyncio.Event()

    @callback
    def _compiled(_event: Event) -> None:
        ready.set()

    unsub = hass.bus.async_listen(EVENT_RECORDER_5MIN_STATISTICS_GENERATED, _compiled)
    try:
        async with asyncio.timeout(COMPILE_WAIT_TIMEOUT.total_seconds()):
            while not await _async_has_five_minute_row_from(hass, statistic_id, window):
                ready.clear()
                await ready.wait()
    except TimeoutError:
        return False
    finally:
        unsub()
    return True


@callback
def async_replace_statistics(
    hass: HomeAssistant,
    statistic_id: str,
    name: str,
    unit: str,
    rows: list[StatisticData],
) -> None:
    """Discard a statistic and import `rows` in its place.

    Both go onto the recorder's queue, so the import can't land first.
    """
    metadata: StatisticMetaData = {
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": name,
        "source": "recorder",
        "statistic_id": statistic_id,
        "unit_class": "energy",
        "unit_of_measurement": unit,
    }
    recorder = get_instance(hass)
    recorder.async_clear_statistics([statistic_id])
    if rows:
        async_import_statistics(hass, metadata, rows)


@callback
def async_shift_statistics(
    hass: HomeAssistant,
    statistic_id: str,
    start: datetime,
    amount: float,
    unit: str,
) -> None:
    """Add `amount` to every sum from `start` on, five-minute and hourly."""
    get_instance(hass).async_adjust_statistics(statistic_id, start, amount, unit)
