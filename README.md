# Clean Energy

A Home Assistant custom component that filters anomalous spikes out of energy sensors.

_Disclosure: Vibe-coded, sorry. Lots of debugging and testing though._

## The problem

Some energy sensors (especially cheaper smart plugs and meters) occasionally report bogus values — enormous jumps like 100,000 kWh in a single reading. These spikes corrupt your Energy Dashboard totals and cost calculations.

## How it works

1. **Passive monitoring**: Once installed, Clean Energy watches all `total_increasing` energy sensors in the background. It never modifies anything without your approval.

2. **Rate-based spike detection**: Instead of a fixed kWh threshold, it calculates the *implied power draw* of each reading. A jump of 10 kWh over 24 hours implies 0.4 kW (normal). A jump of 10 kWh in 3 seconds implies 12,000 kW (bogus). The default threshold is 50 kW — generous enough to cover EV chargers, large appliances, and whole-home monitoring.

3. **Discovery, not auto-correction**: When a spike is detected on a sensor you haven't approved, a discovery notification appears in Home Assistant asking if you'd like to monitor it. **No filtering happens without your explicit approval.**

4. **Per-sensor config entries**: Each approved sensor appears as its own entry under the Clean Energy integration. You can add sensors manually or accept discovery prompts.

5. **A parallel “clean” entity**: For each approved sensor `sensor.foo`, Clean Energy creates a parallel `sensor.foo_clean` holding its own running total. Every plausible increase in the source is added to that total; an increase that implies an impossible power draw is simply not counted. The source entity is left **completely untouched**. You point the Energy Dashboard at the clean entity instead.

6. **Permanent and transient spikes are both handled**: Some flaky meters spike *permanently* — the meter jumps to a bogus value and keeps incrementing from there. Others spike and fall back to the true reading minutes later. Because the clean entity only ever moves by increments it accepted, both work: real consumption on top of a permanently-spiked source is still counted, and a spike that reverts leaves the clean total untouched. If the source falls — a spike reverting, or a genuine meter reset such as a Z-Wave manual reset — the clean total holds steady and re-anchors to the new source level.

7. **State survives restarts**: The running total, the last source reading, and the time it was taken are all restored when Home Assistant restarts, so downtime counts toward the elapsed-time calculation and a meter reporting the energy it banked while HA was down isn't mistaken for a spike.

8. **History backfill**: When the clean entity is first created, the source's existing hourly Long-Term Statistics history is copied over so the dashboard retains continuity when you swap.

## Sensors

For each approved sensor `sensor.foo`, Clean Energy creates one user-facing entity and four diagnostics. Where possible they're attached to the parent sensor's device.

| Entity suffix | Name | Type | Description |
| --- | --- | --- | --- |
| `_clean` | *Foo (Clean)* | Energy (kWh, total increasing) | The replacement entity to use in the Energy Dashboard. Tracks the source's real consumption, skipping every increment it judges impossible. Exposes the cumulative suppressed energy and the last source value as attributes for debugging. The source's prior hourly history is backfilled into this entity at setup. |
| `_last_spike` | *Last Spike* | Timestamp | When the most recent spike on this sensor was detected and filtered. |
| `_last_spike_size` | *Last Spike Size* | Energy (kWh) | Size (kWh implied) of the most recent filtered spike. |
| `_energy_removed` | *Energy Removed* | Energy (kWh, total increasing) | Cumulative kWh suppressed from this sensor by all filtering. |
| `_spike_count` | *Spike Count* | Counter (total increasing) | Number of spike events filtered on this sensor since it was approved. |

## Important: what is and isn't touched

Clean Energy is **non-destructive**. It does not modify the source sensor's state, history, or Long-Term Statistics. The original entity continues to report whatever it reports — spikes and all — and its raw state history will still show those spikes.

What changes is that you now also have a `_clean` companion entity whose values, history, and long-term statistics (LTS) are spike-free going forward. To benefit, **swap your Energy Dashboard source from `sensor.foo` to `sensor.foo_clean`**. (Settings → Dashboards → Energy → edit your grid/individual source.)

**Backfill caveat:** the historical LTS rows that get copied to the clean entity are exactly what the source already had — spikes included. The clean entity gives you spike-free *future* data with continuous historical context. If you'd rather start fresh, just don't swap the dashboard until enough clean history has accumulated, or delete the backfilled statistics for the clean entity from Developer Tools → Statistics.

**Latency:** the clean entity reflects the filter decision in real time. A spike on the source is dropped immediately; a normal reading is counted immediately.

**Never negative:** the clean entity is a `total_increasing` sensor, and Home Assistant's recorder discards *every* statistics row for a `total_increasing` sensor whose state is negative — which leaves the Energy Dashboard blank rather than merely wrong. The accumulator can only move by increments it accepted, so it cannot go negative.

## Setup

### Install via HACS (recommended)

1. In HACS, open the three-dot menu → **Custom repositories**.
2. Add `https://github.com/zacs/ha-clean_energy` with category **Integration**.
3. Find **Clean Energy** in HACS and install it.
4. Restart Home Assistant.
5. Go to **Settings → Devices & Services → Add Integration → Clean Energy**.
6. Set the maximum realistic power draw in kW (default: 50 kW).

### Manual install

1. Copy `custom_components/clean_energy` to your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration → Clean Energy**.
4. Set the maximum realistic power draw in kW (default: 50 kW).
5. That's it — the background monitor is now running. When it detects a spike, you'll get a discovery notification to approve monitoring for that sensor.

To manually add a sensor: go to **Add Integration → Clean Energy** again and select the sensor from the list.

## Adding a sensor manually (without waiting for a spike)

By default, sensors are suggested for monitoring via the discovery flow when the component catches a spike. If you already know a sensor is flaky and don't want to wait, you can add it manually in two ways:

**From the UI:** go to **Settings → Devices & Services → Add Integration → Clean Energy** and pick the sensor from the dropdown. (The first time you add the integration this configures the global threshold; subsequent runs let you add specific sensors.)

**Via service:** call `clean_energy.monitor_sensor` from **Developer Tools → Actions**, an automation, or a script:

```yaml
action: clean_energy.monitor_sensor
data:
  entity_id: sensor.flaky_meter
```

The service validates that the entity exists and is a `total_increasing` energy sensor, and rejects sensors that are already monitored or that belong to Clean Energy itself.

## Configuration

The only setting is **Max realistic power draw (kW)** — the maximum instantaneous power any single sensor could realistically represent. The default of 50 kW covers a 200A residential service (≈48 kW). Adjust this if you have commercial or industrial sensors.

A reading has to fail *both* tests to count as a spike: it must imply more than this power draw, **and** be a jump of at least 1 kWh. The rate test alone is too eager, because readings less than 30 seconds apart are all measured against a 30-second floor — so any jump above roughly 0.4 kWh would imply more than 50 kW. Meters that batch up a reading after an outage legitimately do that, and a genuinely broken meter's spike is orders of magnitude larger than 1 kWh.

## Recovering a clean sensor with bad history

If a clean entity recorded bad values before you upgraded (versions before 0.3.0 could drive it negative, which makes the recorder drop its statistics entirely), the code fix stops it happening again but does not rewrite what was already recorded. To clear the bad rows:

1. Go to **Developer Tools → Statistics**.
2. Find the `_clean` entity and delete its long-term statistics.
3. The entity keeps accumulating correctly from its current value.

Per-sensor entries also have an **initial offset** option (**Settings → Devices & Services → Clean Energy →** the sensor **→ Configure**) that sets how far below the source the clean value starts. Editing it re-seeds the clean entity from the source. An offset larger than the source's current reading is discarded automatically, since the spike it was meant to cancel is evidently no longer in the source.

## Requirements

Home Assistant 2025.11.0 or newer.
