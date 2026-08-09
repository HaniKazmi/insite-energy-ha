# Insite Energy for Home Assistant

A custom component for Home Assistant that tracks your account balance, meter
readings and tariffs from Insite Energy heat networks.

> Requires Home Assistant 2024.11.0 or newer.

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

## Development

```bash
pip install -r requirements_test.txt
pytest
```
