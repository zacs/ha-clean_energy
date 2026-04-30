# Clean Energy

A Home Assistant custom component that filters anomalous spikes out of energy sensors.

_Disclosure: Viibe-coded. Sorry._

## The problem

Some energy sensors (especially cheaper smart plugs and meters) occasionally report bogus values — enormous jumps like 100,000 kWh in a single reading. These spikes corrupt your Energy Dashboard totals and cost calculations.

## How it works

1. **Passive monitoring**: Once installed, Clean Energy watches all `total_increasing` energy sensors in the background. It never modifies anything without your approval.

2. **Rate-based spike detection**: Instead of a fixed kWh threshold, it calculates the *implied power draw* of each reading. A jump of 10 kWh over 24 hours implies 0.4 kW (normal). A jump of 10 kWh in 3 seconds implies 12,000 kW (bogus). The default threshold is 50 kW — generous enough to cover EV chargers, large appliances, and whole-home monitoring.

3. **Discovery, not auto-correction**: When a spike is detected on a sensor you haven't approved, a discovery notification appears in Home Assistant asking if you'd like to monitor it. **No filtering happens without your explicit approval.**

4. **Per-sensor config entries**: Each approved sensor appears as its own entry under the Clean Energy integration. You can add sensors manually or accept discovery prompts.

5. **A parallel “clean” entity, not LTS surgery**: For each approved sensor `sensor.foo`, Clean Energy creates a parallel `sensor.foo_clean` that mirrors the source's value but holds flat across spikes. The source entity is left **completely untouched** (no LTS adjustments, no history rewrites). You point the Energy Dashboard at the clean entity instead.

6. **History backfill**: When the clean entity is first created, the source's existing hourly Long-Term Statistics history is copied over so the dashboard retains continuity when you swap.

## Sensors

For each approved sensor `sensor.foo`, Clean Energy creates one user-facing entity and four diagnostics. Where possible they're attached to the parent sensor's device.

| Entity suffix | Name | Type | Description |
| --- | --- | --- | --- |
| `_clean` | *Foo (Clean)* | Energy (kWh, total increasing) | The replacement entity to use in the Energy Dashboard. Mirrors the source value, but holds flat across detected spikes. The source's prior hourly LTS history is backfilled into this entity at setup. |
| `_last_spike` | *Last Spike* | Timestamp | When the most recent spike on this sensor was detected and filtered. |
| `_last_spike_size` | *Last Spike Size* | Energy (kWh) | Size (kWh implied) of the most recent filtered spike. |
| `_energy_removed` | *Energy Removed* | Energy (kWh, total increasing) | Cumulative kWh suppressed from this sensor by all filtering. |
| `_spike_count` | *Spike Count* | Counter (total increasing) | Number of spike events filtered on this sensor since it was approved. |

## Important: what is and isn't touched

Clean Energy is **non-destructive**. It does not modify the source sensor's state, history, or Long-Term Statistics. The original entity continues to report whatever it reports — spikes and all — and its raw state history will still show those spikes.

What changes is that you now also have a `_clean` companion entity whose values, history, and LTS are spike-free going forward. To benefit, **swap your Energy Dashboard source from `sensor.foo` to `sensor.foo_clean`**. (Settings → Dashboards → Energy → edit your grid/individual source.)

**Backfill caveat:** the historical LTS rows that get copied to the clean entity are exactly what the source already had — spikes included. The clean entity gives you spike-free *future* data with continuous historical context. If you'd rather start fresh, just don't swap the dashboard until enough clean history has accumulated, or delete the backfilled statistics for the clean entity from Developer Tools → Statistics.

**Latency:** the clean entity reflects the filter decision in real time — there's no LTS-correction delay. A spike on the source produces no change on `_clean`; a normal reading is mirrored immediately.

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

By default, sensors are added to monitoring via the discovery flow when the passive watcher catches a spike. If you already know a sensor is flaky and don't want to wait, you can add it manually in two ways:

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
