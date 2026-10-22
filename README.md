# Clean Energy

A Home Assistant custom component that filters anomalous spikes out of energy sensors.

_Disclosure: Vibe-coded, sorry. Lots of debugging and testing though._

## The problem

Some energy sensors (especially cheaper smart plugs and meters) occasionally report bogus values — enormous jumps like 100,000 kWh in a single reading. These spikes corrupt your Energy Dashboard totals and cost calculations.

## How it works

1. **Passive monitoring**: Once installed, Clean Energy watches all `total_increasing` energy sensors in the background, in any energy unit Home Assistant supports. It never modifies anything without your approval.

2. **Rate-based spike detection**: Instead of a fixed kWh threshold, it calculates the *implied power draw* of each reading. A jump of 10 kWh over 24 hours implies 0.4 kW (normal). A jump of 10 kWh in one minute implies 600 kW (bogus). The default threshold is 50 kW — generous enough to cover EV chargers, large appliances, and whole-home monitoring — and each sensor can have its own lower one (see [Configuration](#configuration)).

   The time a meter had to accumulate a jump is measured from when it last *confirmed* its previous value, not when that value first appeared. Most meters re-report an unchanged value periodically, so a lamp that sat idle for 14 hours and then jumps 655 kWh had a minute to do it, not 14 hours.

3. **Discovery, not auto-correction**: When a spike is detected on a sensor you haven't approved, a discovery notification appears in Home Assistant asking if you'd like to monitor it. **No filtering happens without your explicit approval.**

4. **Per-sensor config entries**: Each approved sensor appears as its own entry under the Clean Energy integration. Sensors arrive by accepting a discovery prompt or through the `clean_energy.monitor_sensor` action (see [Adding a sensor manually](#adding-a-sensor-manually-without-waiting-for-a-spike)).

5. **A parallel “clean” entity**: For each approved sensor `sensor.foo`, Clean Energy creates a parallel `sensor.foo_clean` holding its own running total. Every plausible increase in the source is added to that total; an increase that implies an impossible power draw is simply not counted. The source entity is left **completely untouched**. You point the Energy Dashboard at the clean entity instead.

6. **Permanent and transient spikes are both handled**: Some flaky meters spike *permanently* — the meter jumps to a bogus value and keeps incrementing from there. Others spike and fall back to the true reading minutes later. Because the clean entity only ever moves by increments it accepted, both work: real consumption on top of a permanently-spiked source is still counted, and a spike that reverts leaves the clean total untouched. If the source falls — a spike reverting, or a genuine meter reset such as a Z-Wave manual reset — the clean total holds steady and re-anchors to the new source level.

7. **State survives restarts**: The running total, the last source reading, and the time it was taken are all restored when Home Assistant restarts, so downtime counts toward the elapsed-time calculation and a meter reporting the energy it banked while HA was down isn't mistaken for a spike.

8. **Clean history from day one**: A new clean entity doesn't start empty, and it doesn't copy its source's history verbatim either. It rebuilds the source's hourly Long-Term Statistics with the same spike test, drops the hours spikes landed in, and starts its running total at the level that history implies. That includes the spike that triggered discovery in the first place — discovery only ever fires *because* of a spike, so that spike is always in the history. The dashboard stays continuous across the switch.

9. **Follows its source**: Rename the source sensor (renaming a device renames its entities) or move it to another device, and the clean entity follows. It keeps its own entity ID either way.

## Sensors

For each approved sensor `sensor.foo`, Clean Energy creates one user-facing entity and four diagnostics, linked to the source sensor's device when it has one.

| Entity suffix | Name | Type | Description |
| --- | --- | --- | --- |
| `_clean` | *Foo (Clean)* | Energy (total increasing, source's unit) | The replacement entity to use in the Energy Dashboard. Tracks the source's real consumption, skipping every increment it judges impossible. Attributes expose the energy suppressed so far, the last source value and the power ceiling in force, for debugging. |
| `_last_spike` | *Last Spike* | Timestamp | When the most recent spike on this sensor was detected and filtered. |
| `_last_spike_size` | *Last Spike Size* | Energy (kWh) | Size (kWh implied) of the most recent filtered spike. |
| `_energy_removed` | *Energy Removed* | Energy (kWh, total increasing) | Cumulative kWh suppressed from this sensor by live filtering. |
| `_spike_count` | *Spike Count* | Counter (total increasing) | Number of spike events filtered on this sensor since it was approved. |

## Important: what is and isn't touched

Clean Energy is **non-destructive**. It does not modify the source sensor's state, history, or Long-Term Statistics. The original entity continues to report whatever it reports — spikes and all — and its raw state history will still show those spikes.

What changes is that you now also have a `_clean` companion entity whose values, history, and long-term statistics (LTS) are spike-free. To benefit, **swap your Energy Dashboard source from `sensor.foo` to `sensor.foo_clean`**. (Settings → Dashboards → Energy → edit your grid/individual source.)

**How history is filtered:** history only has hourly resolution, so it catches spikes that are large over an hour (more than the power ceiling's worth of energy in one hour: 50 kWh at the default). The live filter sees every reading and catches far smaller ones. When an hour is dropped, so is that hour's real consumption, which is negligible next to the spike.

**When history appears:** the clean entity has its starting value within seconds; its rebuilt history follows within about ten minutes. The recorder compiles statistics every five minutes, and the rebuild has to work around those compiles so that it doesn't make the recorder count the switch as consumption.

**Latency:** the clean entity reflects the filter decision in real time. A spike on the source is dropped immediately; a normal reading is counted immediately.

**Never negative:** the clean entity is a `total_increasing` sensor, and Home Assistant's recorder refuses to accumulate a negative one — it raises on every reading after the first and compiles the hour with a `sum` of 0, so the Energy Dashboard reads zero for as long as the state stays negative. The accumulator can only move by increments it accepted, so it cannot go negative.

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

## Adding a sensor manually (without waiting for a spike)

By default, sensors are suggested for monitoring via the discovery flow when the component catches a spike. If you already know a sensor is flaky and don't want to wait, call `clean_energy.monitor_sensor` from **Developer Tools → Actions**, an automation, or a script:

```yaml
action: clean_energy.monitor_sensor
data:
  entity_id: sensor.flaky_meter
```

The action validates that the entity exists and is a `total_increasing` energy sensor, and rejects sensors that are already monitored or that belong to Clean Energy itself. If the sensor's reading already contains a spike, the new clean entity leaves it out: its starting value comes from the source's filtered history, not from the current reading.

(Adding the integration a second time from **Add Integration** isn't a way to add a sensor; Clean Energy is a single integration with one entry per monitored sensor.)

## Configuration

**Max realistic power draw (kW)** — the most power any single sensor could plausibly represent. Set on the integration itself (**Settings → Devices & Services → Clean Energy → Configure**), it applies to discovery and to every monitored sensor. The default of 50 kW covers a 200A residential service (≈48 kW). Adjust it if you have commercial or industrial sensors.

**Per-sensor ceiling** — each monitored sensor's own **Configure** dialog takes an optional ceiling for that sensor alone. A ceiling sized for a whole house lets moderate glitches through on small devices: a 655 kWh jump spread over 14 hours implies 46.8 kW, plausible for a house, absurd for a lamp. Smart plugs can't draw more than a few kW, so a ceiling of 3–4 kW catches far more. Leave it empty to use the global value.

A reading has to fail *both* tests to count as a spike: it must imply more than the ceiling, **and** be a jump of at least 1 kWh. The rate test alone is too eager, because readings less than 30 seconds apart are all measured against a 30-second floor — so any jump above roughly 0.4 kWh would imply more than 50 kW. Meters that batch up a reading after an outage legitimately do that, and a genuinely broken meter's spike is orders of magnitude larger than 1 kWh.

## Rebuilding a clean sensor's history

`clean_energy.rebuild_history` re-derives a clean sensor's Long-Term Statistics from its source's, leaving out impossible jumps, and corrects its current value to match:

```yaml
action: clean_energy.rebuild_history
target:
  entity_id: sensor.flaky_meter_clean
```

It responds with what it found — the hours dropped, the energy removed, and the value before and after — and replaces the statistics over the next ten minutes or so. Target several clean sensors, or all of them, to rebuild them together.

- **`offset`** (optional): energy, in the source's unit, that you know is bogus in the source's current reading. Use it for a spike older than the source's recorded history, which nothing can detect. It overrides the value derived from history.
- **`force`** (optional): the rebuild refuses if the clean sensor has statistics older than anything recorded for its source, since replacing them would lose that history. Pass `force: true` to rebuild anyway.

## Upgrading to 0.4.0

**Duplicate devices are removed automatically.** Versions before 0.4.0 joined the source's device instead of linking to it. Home Assistant 2026.8 made devices single-config-entry and split each such device in two, leaving a Clean Energy copy that listed the integration but showed "This device has no entities". On startup, 0.4.0 deletes that copy and puts the entities back on the real device under the entity IDs they already had.

**Existing clean sensors keep their history until you ask.** Clean sensors created before 0.4.0 copied their source's history with spikes included — often the very spike that led you to add them, which is why they could look no different from their source. Their statistics are left alone on upgrade, because rebuilding rewrites them. To clean them up, run the rebuild once:

```yaml
action: clean_energy.rebuild_history
target:
  entity_id: all
```

The source's statistics are never touched, so the rebuild can be repeated.

**The initial offset option is gone.** Earlier versions let you edit a sensor's offset to repair it. Applying it changed the running total in place, and the recorder counts any change of a running total as consumption: lowering a clean sensor from 5,000 to 38 kWh added 38 kWh to its statistics, a fresh spike made by the repair. The `offset` field of `rebuild_history` does the same job safely.

## Requirements

Home Assistant 2026.8.0 or newer.
