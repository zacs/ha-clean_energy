"""History rebuilds, end to end, against the real recorder.

Each test drives the recorder's five-minute compiles by hand, on a timeline
a day ahead of the wall clock: the recorder compiles real time at startup,
and a frozen clock inside the current hour would collide with those runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import EVENT_RECORDER_5MIN_STATISTICS_GENERATED
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceNotSupported, ServiceValidationError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.clean_energy.const import (
    CONF_ENTITY_ID,
    DOMAIN,
    HISTORY_DONE_KEY,
    LEGACY_BACKFILL_DONE_KEY,
    SERVICE_REBUILD_HISTORY,
)

from .test_setup import _hub

SOURCE = "sensor.lamp_energy"
CLEAN = "sensor.lamp_energy_clean"
SPIKE = 234881286.0
# Six hours of +0.05 kWh from 38.0, one of them spiked: the spike hour is
# dropped whole, taking its own 0.05 kWh of real use with it.
CLEAN_LEVEL = 38.0 + 0.05 * 5
ATTRS = {
    "unit_of_measurement": "kWh",
    "state_class": "total_increasing",
    "device_class": "energy",
    "friendly_name": "Lamp Energy",
}


def _top() -> datetime:
    return dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )


def _metadata(statistic_id: str) -> dict:
    return {
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": statistic_id,
        "source": "recorder",
        "statistic_id": statistic_id,
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }


async def _import_hours(
    hass: HomeAssistant, statistic_id: str, top: datetime, *, spike_hour: int | None
) -> float:
    """Six hours of +0.05 kWh ending at `top`; returns the final reading."""
    state, total, rows = 38.0, 0.0, []
    for i in range(6):
        inc = 0.05 + (SPIKE if i == spike_hour else 0.0)
        state += inc
        total += inc
        rows.append(
            {"start": top - timedelta(hours=6 - i), "state": state, "sum": total}
        )
    async_import_statistics(hass, _metadata(statistic_id), rows)
    await async_wait_recording_done(hass)
    return state


class _Timeline:
    """Drives frozen time and the recorder's compiles."""

    def __init__(
        self, hass: HomeAssistant, freezer: FrozenDateTimeFactory, top: datetime
    ):
        self.hass, self.freezer, self.top = hass, freezer, top
        self.source = 0.0

    async def at(self, minute: float) -> None:
        moment = self.top + timedelta(minutes=minute)
        self.freezer.move_to(moment)
        async_fire_time_changed(self.hass, moment)
        await self.hass.async_block_till_done()

    async def reading(self, value: float) -> None:
        self.source = value
        self.hass.states.async_set(SOURCE, f"{value:.6f}", ATTRS)
        await self.hass.async_block_till_done()
        await async_wait_recording_done(self.hass)

    async def compile(self, window_minute: int) -> None:
        """Compile the five-minute window starting at `window_minute`."""
        await self.at(window_minute + 5 + 10 / 60)
        do_adhoc_statistics(
            self.hass, start=self.top + timedelta(minutes=window_minute)
        )
        await async_wait_recording_done(self.hass)
        self.hass.bus.async_fire(EVENT_RECORDER_5MIN_STATISTICS_GENERATED)
        await self.hass.async_block_till_done()
        await async_wait_recording_done(self.hass)

    async def run_hour(self, from_window: int) -> None:
        """Consume a little every window, compiling each, to the hour's end."""
        for window in range(from_window, 60, 5):
            await self.at(window + 2)
            await self.reading(self.source + 0.004)
            await self.compile(window)


async def _until(hass: HomeAssistant, what: str, check) -> None:
    """Let the recorder and the background rebuild run until `check()` holds."""
    for _ in range(100):
        if await check():
            return
        await async_wait_recording_done(hass)
        await hass.async_block_till_done()
    raise AssertionError(f"timed out waiting for {what}")


async def _settle(hass: HomeAssistant) -> None:
    """Let the background rebuild get as far as it can at the current time.

    In production the clock doesn't jump: whatever the rebuild does next
    happens before the next compile. Advancing frozen time while its history
    fetch is still on the recorder's thread reorders that, and once hid
    a broken sequence by accident.
    """
    for _ in range(20):
        await async_wait_recording_done(hass)
        await hass.async_block_till_done()


async def _history_imported(hass: HomeAssistant, top: datetime) -> None:
    """Wait for the rebuild to clear and re-import the clean sensor's hours."""

    async def check() -> bool:
        rows = await _hourly(hass, CLEAN, top)
        return len(rows) == 6 and max(r["change"] for r in rows) < 1.0

    await _until(hass, "the derived history to be imported", check)


async def _rebuilt(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    async def check() -> bool:
        return bool(entry.data.get(HISTORY_DONE_KEY))

    await _until(hass, "the rebuild to finish", check)


async def _seeded(hass: HomeAssistant) -> float:
    """Wait for the clean sensor's history-derived starting value.

    The first rebuild is a background task, which async_block_till_done
    deliberately doesn't wait for, and its history fetch runs on the
    recorder's thread; let both settle rather than assume they have.
    """
    for _ in range(100):
        state = hass.states.get(CLEAN)
        if state is not None and state.state not in ("unknown", "unavailable"):
            return float(state.state)
        await async_wait_recording_done(hass)
        await hass.async_block_till_done()
    raise AssertionError(f"{CLEAN} was never seeded")


async def _hourly(hass: HomeAssistant, statistic_id: str, top: datetime) -> list[dict]:
    def _fetch():
        return statistics_during_period(
            hass,
            top - timedelta(hours=12),
            None,
            {statistic_id},
            "hour",
            None,
            {"sum", "state", "change"},
        )

    return (await hass.async_add_executor_job(_fetch)).get(statistic_id, [])


def _assert_clean_series(rows: list[dict]) -> None:
    """Every hour consumed a little, none a lot, and none negatively."""
    changes = [row["change"] for row in rows]
    assert len(changes) >= 7, changes
    assert all(0 <= change < 1.0 for change in changes), changes


@pytest.fixture
async def sensor_platform(recorder_mock, hass: HomeAssistant) -> None:
    """The recorder only compiles sensor statistics with this loaded."""
    await async_setup_component(hass, "sensor", {})


async def test_new_sensor_history_excludes_the_spike_that_found_it(
    sensor_platform, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Discovery fires *because* of a spike, so it's always in the history.

    Copying that history verbatim reproduced the spike in the clean sensor,
    which then looked no different from its source; and importing hourly
    rows alone left the recorder restarting the sum at 0 straight after them.
    """
    top = _top()
    timeline = _Timeline(hass, freezer, top)
    await timeline.at(2)
    current = await _import_hours(hass, SOURCE, top, spike_hour=3)
    await timeline.reading(current)

    hub = _hub(hass)
    hub.add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTITY_ID: SOURCE}, unique_id=SOURCE
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()

    # Seeded at the clean level, never at the spiked reading.
    assert await _seeded(hass) == pytest.approx(CLEAN_LEVEL)

    await _settle(hass)
    await timeline.compile(0)  # the window the seed landed in
    await timeline.at(5.6)  # clear of the boundary: history is replaced now
    await _history_imported(hass, top)
    await timeline.compile(5)  # the recorder restarts at 0; we lift it
    await _rebuilt(hass, entry)
    await timeline.run_hour(10)
    _assert_clean_series(await _hourly(hass, CLEAN, top))


async def test_monitoring_an_already_spiked_sensor(
    sensor_platform, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Added by hand with the spike already in the reading.

    That's exactly when someone reaches for monitor_sensor, and with no
    discovery offset to subtract the clean sensor used to mirror the spike.
    """
    top = _top()
    timeline = _Timeline(hass, freezer, top)
    await timeline.at(2)
    current = await _import_hours(hass, SOURCE, top, spike_hour=1)
    await timeline.reading(current)

    hub = _hub(hass)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        DOMAIN, "monitor_sensor", {"entity_id": SOURCE}, blocking=True
    )
    await hass.async_block_till_done()

    assert float(hass.states.get(SOURCE).state) > SPIKE
    assert await _seeded(hass) == pytest.approx(CLEAN_LEVEL)


async def _legacy_install(
    hass: HomeAssistant, timeline: _Timeline, top: datetime
) -> MockConfigEntry:
    """A pre-0.4.0 sensor: spike copied into its history and its value."""
    current = await _import_hours(hass, SOURCE, top, spike_hour=3)
    await _import_hours(hass, CLEAN, top, spike_hour=3)
    hub = _hub(hass)
    hub.add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: SOURCE, LEGACY_BACKFILL_DONE_KEY: True},
        unique_id=SOURCE,
    )
    entry.add_to_hass(hass)
    await timeline.reading(current)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_upgrade_leaves_legacy_history_alone(
    sensor_platform, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Rewriting existing statistics is the user's call, not the upgrade's."""
    top = _top()
    timeline = _Timeline(hass, freezer, top)
    await timeline.at(2)
    entry = await _legacy_install(hass, timeline, top)

    assert HISTORY_DONE_KEY not in entry.data
    rows = await _hourly(hass, CLEAN, top)
    assert max(row["change"] for row in rows) > SPIKE / 2


async def test_rebuilding_a_legacy_sensor(
    sensor_platform, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The fix for existing sensors: history rebuilt, value corrected.

    Correcting the value in place is what the old offset option did, and the
    recorder counted the correction as consumption. Here it must not.
    """
    top = _top()
    timeline = _Timeline(hass, freezer, top)
    await timeline.at(2)
    entry = await _legacy_install(hass, timeline, top)
    assert float(hass.states.get(CLEAN).state) > SPIKE  # mirrors the source
    await timeline.compile(0)

    await timeline.at(7)
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_REBUILD_HISTORY,
        {},
        target={"entity_id": CLEAN},
        blocking=True,
        return_response=True,
    )
    result = response[CLEAN]
    assert result["value_corrected"] is True
    assert result["value_after"] == pytest.approx(CLEAN_LEVEL)
    assert result["energy_removed"] == pytest.approx(SPIKE)
    assert float(hass.states.get(CLEAN).state) == pytest.approx(CLEAN_LEVEL)

    # The rebuild must hold off clearing until the correction's window is
    # compiled; were it to clear now, that window would start from the old
    # value and count the correction as consumption.
    await _settle(hass)
    await timeline.compile(5)  # the window the correction landed in
    await timeline.at(10.6)
    await _history_imported(hass, top)
    await timeline.compile(10)  # restarted at 0 after the clear; lifted
    await _rebuilt(hass, entry)
    await timeline.run_hour(15)
    _assert_clean_series(await _hourly(hass, CLEAN, top))


async def test_rebuild_refuses_to_discard_history_the_source_lacks(
    sensor_platform, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """If the clean sensor remembers more than its source, keep it."""
    top = _top()
    timeline = _Timeline(hass, freezer, top)
    await timeline.at(2)
    await _legacy_install(hass, timeline, top)
    async_import_statistics(
        hass,
        _metadata(CLEAN),
        [{"start": top - timedelta(days=30), "state": 1.0, "sum": 0.0}],
    )
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError, match="force"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REBUILD_HISTORY,
            {},
            target={"entity_id": CLEAN},
            blocking=True,
            return_response=True,
        )


async def test_rebuild_targets_only_clean_sensors(
    sensor_platform, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Naming a diagnostic sensor is refused before anything runs."""
    top = _top()
    timeline = _Timeline(hass, freezer, top)
    await timeline.at(2)
    await _legacy_install(hass, timeline, top)

    with pytest.raises(ServiceNotSupported):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REBUILD_HISTORY,
            {},
            target={"entity_id": "sensor.lamp_energy_spike_count"},
            blocking=True,
            return_response=True,
        )


async def test_rebuild_all(
    sensor_platform, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """`entity_id: all` rebuilds every clean sensor and skips diagnostics.

    The four diagnostic sensors per entry are targets too, and one refusal
    used to fail the whole action, after other sensors' rebuilds had started.
    """
    top = _top()
    timeline = _Timeline(hass, freezer, top)
    await timeline.at(2)
    await _legacy_install(hass, timeline, top)
    other = "sensor.fridge_energy"
    hass.states.async_set(other, "12.0", ATTRS)
    second = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: other, LEGACY_BACKFILL_DONE_KEY: True},
        unique_id=other,
    )
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()

    await timeline.at(7)
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_REBUILD_HISTORY,
        {},
        target={"entity_id": "all"},
        blocking=True,
        return_response=True,
    )
    assert set(response) == {CLEAN, "sensor.fridge_energy_clean"}
