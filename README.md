# Insite Energy for Home Assistant

A custom component for Home Assistant that tracks your account balance, meter
readings and tariffs from Insite Energy heat networks.

> Requires Home Assistant 2025.11.0 or newer — the spread statistics set `unit_class`
> on their metadata, which recorder only gained in that release.

## Installation via HACS

1. Open HACS in Home Assistant.
2. Go to Integrations > Menu (3 dots) > Custom repositories.
3. Add the URL of this repository and choose "Integration" as the category.
4. Click Add and then search for "Insite Energy" to install.
5. Restart Home Assistant.

## Configuration

Once installed, go to **Settings -> Devices & Services**, click **Add Integration**, and search for **Insite Energy**. You will be prompted to enter your Insite Energy email and password. One account is supported per Home Assistant instance.

To change your credentials or the polling interval later, click **Configure** on the integration page. Leave the password field blank to keep the one already stored.

If your password stops working, Home Assistant will prompt you to re-enter it rather than failing quietly in the background.

## Entities Provided

The integration automatically creates devices for your Account and each Utility Meter found on your account.

### Account Details Device
- **Balance**: A sensor showing your current account balance in GBP, matching the portal (this can be negative).
- **Last Poll Time** (Diagnostic): A timestamp showing exactly when data was last successfully retrieved from the Insite Energy API.

### Utility Devices (e.g., Heating & Hot Water)
For each utility listed on your account, a separate device is created with the following sensors:
- **Meter Reading**: The latest meter reading in kWh.
- **Rate**: Your current unit rate, in GBP/kWh. The portal quotes this in pence, so `14.67p` appears here as `0.1467`.
- **Standing Charge**: The daily standing charge, in GBP/day, converted the same way.
- **Last Reading Date**: A timestamp for when your meter was last read.
- **Meter Serial Number** (Diagnostic): The serial number of the utility meter.

**Meter Reading** is published as an increasing energy total, so it can be added to the Energy dashboard as an individual device. Note the caveat below: readings arrive in infrequent jumps rather than continuously, so consumption shows up as a spike on whichever day the portal updates.

## Data Refresh

By default, the integration polls your account every 12 hours. You can set any interval between 1 and 168 hours by clicking **Configure** on the integration page.

**Note on Update Frequency:** While you can set the integration to poll more frequently, please be aware that Insite Energy typically only synchronizes meter readings with their online portal at best once a day. I've seen it be once a month in some instances. Setting a very short update interval will not result in real-time data, but may hit rate limits.

You can also manually force a data refresh using the `insite_energy.refresh_data` service in Home Assistant.

### A note on speed

Logging in to the Insite Energy site takes anywhere from a few seconds to well over half a minute. To keep that off Home Assistant's startup path, the last successful response is cached to disk: after a restart your entities come back immediately with their previous values and refresh in the background. Within a session, polls reuse the existing login where they can, which takes under a second.

### A note on the Energy dashboard

Once Home Assistant has finished starting, **Meter Reading** re-publishes its cached value before the first refresh runs. That looks like a pointless write, but it is deliberate.

If you cost a source using a price entity, the Energy dashboard computes the cost itself and accrues nothing on the first meter event it sees — it takes that reading as a baseline and stops there. It only ever initialises from a state change, and it registers its listener after this integration has already created its entities. Left alone, the first event it sees is a real meter increment, and because these meters move roughly once a day, that whole day is charged nothing.

Re-publishing an unchanged reading gives it a harmless baseline instead. Doing it *before* the refresh is what makes a meter that moved while Home Assistant was down still get charged: the newer reading then arrives as a genuine delta rather than being swallowed as the baseline.

## Spread statistics

Readings arrive long after the fact, covering everything since the previous reading — a month at a time for some utilities. Home Assistant attributes the whole delta to the hour the reading happened to arrive, so the Energy dashboard shows a month of usage as a single spike.

Because **Last Reading Date** says which period a reading actually covers, the integration also publishes that consumption spread across the hours it belongs to, as external statistics:

| Statistic | Unit |
| --- | --- |
| `insite_energy:<utility>_energy` | kWh |
| `insite_energy:<utility>_cost` | GBP |

`<utility>` is the portal's ShortName in lower case, so `insite_energy:hh_energy` for Heating & Hot Water. Add these to the Energy dashboard **instead of** the Meter Reading entity — not as well as, or everything is counted twice.

These have to be external statistics rather than the entity's own. The period is over by the time we learn of it, so there is no live value a sensor could report, and the recorder would overwrite anything written to an entity-backed statistic.

### Weighting by activity

By default a reading is spread evenly. If you have something that indicates when the utility was actually being used, the spread can follow it instead, so energy lands on the hours it was really consumed.

Pick one statistic per utility under **Configure**. The picker lists what the recorder actually holds long-term statistics for, which is the right question — a weight is read back weeks later, so anything without them is useless. For an ordinary sensor the statistic is just its entity id.

Both kinds work: a `measurement` statistic contributes its hourly mean, a cumulative one contributes the rise across each hour — so a meter can be used directly, which is more accurate than its flow sensor.

The picker cannot be filtered, so it also offers this integration's own `insite_energy:<utility>_energy` and `_cost` statistics. **Do not pick those.** Weighting a utility by what was published for it would make each spread a copy of the last one's shape, drifting further from reality every time. The integration refuses them and spreads evenly instead, with a warning in the log, but it is easier not to choose them.

One sensor per utility is deliberate. If the signal is a combination — hot water *and* space heating, say — build a template sensor that expresses it, which it can do far better than any list of weights this integration could offer.

Two things worth knowing. The signal must be a **sensor, not a binary_sensor**: state history is purged after ten days, but long-term statistics are kept forever, and a monthly reading needs weights from weeks ago. And hours with no recorded activity weigh nothing, so they receive no energy — their share moves to the hours that were active. If nothing at all is recorded for the whole period, the reading is spread evenly instead.

Weighting only ever changes *where* the energy lands. The published rows always sum to exactly the meter delta.

## Development

```bash
pip install -r requirements_test.txt
pytest
```
