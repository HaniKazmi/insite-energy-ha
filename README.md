# Insite Energy for Home Assistant

A custom component for Home Assistant that tracks your Account Balance and Top-up Details from Insite Energy heat networks.

## Installation via HACS

1. Open HACS in Home Assistant.
2. Go to Integrations > Menu (3 dots) > Custom repositories.
3. Add the URL of this repository and choose "Integration" as the category.
4. Click Add and then search for "Insite Energy" to install.
5. Restart Home Assistant.

## Configuration

Once installed, go to **Settings -> Devices & Services** in Home Assistant, click **Add Integration**, and search for **Insite Energy**. You will be prompted to enter your Insite Energy email and password.

## Entities Provided

The integration automatically creates devices for your Account and each Utility Meter found on your account.

### Account Details Device
- **Balance**: A sensor showing your current account balance in GBP.
- **Last Poll Time** (Diagnostic): A timestamp showing exactly when data was last successfully retrieved from the Insite Energy API.

### Utility Devices (e.g., Heating & Hot Water)
For each utility listed on your account, a separate device is created with the following sensors:
- **Meter Reading**: The latest meter reading in kWh.
- **Rate**: Your current tariff/rate for this utility.
- **Standing Charge**: The standing charge value for this utility.
- **Last Reading Date**: The date your meter was last read.
- **Meter Serial Number** (Diagnostic): The serial number of the utility meter.

## Data Refresh

By default, the integration polls your account every 12 hours. You can customize this interval during setup or by clicking **Configure** on the integration page. 

**Note on Update Frequency:** While you can set the integration to poll more frequently, please be aware that Insite Energy typically only synchronizes meter readings with their online portal at best once a day. I've seen it be once a month in some instances. Setting a very short update interval will not result in real-time data, but may hit rate limits.

You can also manually force a data refresh using the `insite_energy.refresh_data` service in Home Assistant.
