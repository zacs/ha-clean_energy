# Clean Energy

A Home Assistant custom component that detects and corrects anomalous spikes in energy sensors.

_Disclosure: Viibe-coded. Sorry._

## The problem

Some energy sensors (especially cheaper smart plugs and meters) occasionally report bogus values — enormous jumps like 100,000 kWh in a single reading. These spikes corrupt your Energy Dashboard totals and cost calculations.

## How it works

1. **Passive monitoring**: Once installed, Clean Energy watches all `total_increasing` energy sensors in the background. It never modifies anything without your approval.

2. **Rate-based spike detection**: Instead of a fixed kWh threshold, it calculates the *implied power draw* of each reading. A jump of 10 kWh over 24 hours implies 0.4 kW (normal). A jump of 10 kWh in 3 seconds implies 12,000 kW (bogus). The default threshold is 50 kW — generous enough to cover EV chargers, large appliances, and whole-home monitoring.

3. **Discovery, not auto-correction**: When a spike is detected on a sensor you haven't approved, a discovery notification appears in Home Assistant asking if you'd like to monitor it. **No corrections are ever made without your explicit approval.**

4. **Per-sensor config entries**: Each approved sensor appears as its own entry under the Clean Energy integration. You can add sensors manually or accept discovery prompts. Only approved sensors get corrections.

5. **Retroactive first correction**: When you approve a discovered sensor, the spike that triggered the discovery is corrected immediately — you don't lose that first one.

6. **Statistics correction**: For approved sensors, spikes are corrected by adjusting the Long-Term Statistics (LTS) sum via `recorder.adjust_statistics`. This is the same data the Energy Dashboard reads.

## Sensors

For each approved sensor, Clean Energy creates four diagnostic entities (named after the parent sensor, e.g. `sensor.flaky_meter_energy_removed`). Where possible they're attached to the parent sensor's device.

| Entity suffix | Name | Type | Description |
| --- | --- | --- | --- |
| `_last_spike` | *Last Spike* | Timestamp | When the most recent spike on this sensor was detected and corrected. |
| `_last_spike_size` | *Last Spike Size* | Energy (kWh) | Size of the most recent corrected spike. |
| `_energy_removed` | *Energy Removed* | Energy (kWh, total increasing) | Cumulative kWh removed from this sensor's Long-Term Statistics by all corrections. Useful for the Energy Dashboard if you want to see how much bogus energy was filtered out. |
| `_spike_count` | *Spike Count* | Counter (total increasing) | Number of spikes corrected on this sensor since it was approved. |

## Important: what gets corrected and what doesn't

Clean Energy corrects the **Long-Term Statistics sum**, which is what powers the Energy Dashboard's totals and cost calculations. This means your energy totals, daily/monthly/yearly summaries, and cost tracking will be accurate.

However, the **raw state history** (the line graph you see when clicking on an entity) will still show the spike. This is cosmetic — those raw state values are recorded by the recorder before Clean Energy can intervene, and modifying the state history database directly would be fragile and risky. The data that matters (your energy totals and costs) will be correct.

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
