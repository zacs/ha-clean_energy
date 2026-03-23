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

## Important: what gets corrected and what doesn't

Clean Energy corrects the **Long-Term Statistics sum**, which is what powers the Energy Dashboard's totals and cost calculations. This means your energy totals, daily/monthly/yearly summaries, and cost tracking will be accurate.

However, the **raw state history** (the line graph you see when clicking on an entity) will still show the spike. This is cosmetic — those raw state values are recorded by the recorder before Clean Energy can intervene, and modifying the state history database directly would be fragile and risky. The data that matters (your energy totals and costs) will be correct.

## Setup

1. Copy `custom_components/clean_energy` to your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration → Clean Energy**.
4. Set the maximum realistic power draw in kW (default: 50 kW).
5. That's it — the background monitor is now running. When it detects a spike, you'll get a discovery notification to approve monitoring for that sensor.

To manually add a sensor: go to **Add Integration → Clean Energy** again and select the sensor from the list.

## Configuration

The only setting is **Max realistic power draw (kW)** — the maximum instantaneous power any single sensor could realistically represent. The default of 50 kW covers a 200A residential service (≈48 kW). Adjust this if you have commercial or industrial sensors.
