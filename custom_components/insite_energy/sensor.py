"""Sensor platform for Insite Energy."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import InsiteEnergyDataUpdateCoordinator
from .util import parse_pence, parse_reading_date, utility_key

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Insite Energy sensor platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Account Device Sensors
    async_add_entities(
        [
            InsiteAccountBalanceSensor(coordinator),
            InsiteAccountLastPollSensor(coordinator),
        ]
    )

    # Utility Devices Sensors. At startup these are created from the cached
    # snapshot of the previous run, so watch for utilities that only show up
    # once the first live refresh lands.
    known_utilities: set[str] = set()

    @callback
    def _async_add_utilities() -> None:
        """Add sensors for any utility we haven't seen yet."""
        view_model = coordinator.data or {}
        entities: list[SensorEntity] = []
        seen_this_pass: set[str] = set()

        for utility in view_model.get("UtilityDetails") or []:
            name = utility.get("Name")
            if not name:
                continue
            key = utility_key(utility)
            if key in seen_this_pass:
                # Two utilities sharing a ShortName would otherwise vanish
                # without a trace. Not seen in the wild, but make it loud.
                _LOGGER.warning(
                    "Skipping utility %s: it shares the identifier '%s' with "
                    "another utility on this account",
                    name,
                    key,
                )
                continue
            seen_this_pass.add(key)
            if key in known_utilities:
                continue
            known_utilities.add(key)
            entities.extend(
                [
                    InsiteUtilityReadingSensor(coordinator, key, name),
                    InsiteUtilityRateSensor(coordinator, key, name),
                    InsiteUtilityStandingChargeSensor(coordinator, key, name),
                    InsiteUtilityReadingDateSensor(coordinator, key, name),
                    InsiteUtilitySerialNumberSensor(coordinator, key, name),
                ]
            )

        if entities:
            async_add_entities(entities)

    _async_add_utilities()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_utilities))


class InsiteEnergyBaseEntity(CoordinatorEntity):
    """Base entity for Insite Energy."""

    def __init__(self, coordinator: InsiteEnergyDataUpdateCoordinator) -> None:
        """Initialize the base entity."""
        super().__init__(coordinator)
        # Keyed on the config entry rather than the email address, which the
        # user can change in the options flow.
        self._base_id = coordinator.config_entry.entry_id


class InsiteAccountEntity(InsiteEnergyBaseEntity):
    """Base entity for the Account Details device."""

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, f"{self._base_id}_account")},
            "name": "Account Details",
            "manufacturer": "Insite Energy",
        }


class InsiteUtilityEntity(InsiteEnergyBaseEntity):
    """Base entity for Utility devices."""

    def __init__(
        self,
        coordinator: InsiteEnergyDataUpdateCoordinator,
        utility_key: str,
        utility_name: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.utility_key = utility_key
        self._utility_name = utility_name

    @property
    def _display_name(self) -> str:
        """Current name of the utility, tracking upstream renames."""
        data = self._get_utility_data()
        if data and data.get("Name"):
            return str(data["Name"])
        return self._utility_name

    @property
    def device_info(self):
        """Return device information."""
        serial = ""
        data = self._get_utility_data()
        if data and data.get("MeterSerialNumber"):
            serial = f" ({data.get('MeterSerialNumber')})"

        return {
            "identifiers": {(DOMAIN, f"{self._base_id}_{self.utility_key}")},
            "name": f"{self._display_name}{serial}",
            "manufacturer": "Insite Energy",
            "model": "Utility Meter",
        }

    def _get_utility_data(self):
        """Helper to find the specific utility dict."""
        for utility in (self.coordinator.data or {}).get("UtilityDetails") or []:
            if utility_key(utility) == self.utility_key:
                return utility
        return None


# --- Account Sensors ---


class InsiteAccountBalanceSensor(InsiteAccountEntity, SensorEntity):
    """Representation of Account Details (Balance)."""

    _attr_has_entity_name = True
    _attr_name = "Balance"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:cash"

    def __init__(self, coordinator: InsiteEnergyDataUpdateCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._base_id}_account_balance"
        self._attr_native_unit_of_measurement = "GBP"

    @property
    def native_value(self):
        """Return the state."""
        if self.coordinator.data:
            balance_str = self.coordinator.data.get("CreditAccountBalance")
            if balance_str:
                try:
                    return float(balance_str)
                except ValueError:
                    pass
        return None


class InsiteAccountLastPollSensor(InsiteAccountEntity, SensorEntity):
    """Representation of Account Details (Last Poll Time)."""

    _attr_has_entity_name = True
    _attr_name = "Last Poll Time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: InsiteEnergyDataUpdateCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._base_id}_account_last_poll"

    @property
    def native_value(self):
        """Return the state."""
        if self.coordinator.data:
            return self.coordinator.data.get("_last_poll_time")
        return None


# --- Utility Sensors ---


class InsiteUtilityReadingSensor(InsiteUtilityEntity, SensorEntity):
    """Utility Meter Reading Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Meter Reading"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, utility_key, utility_name):
        """Initialize."""
        super().__init__(coordinator, utility_key, utility_name)
        self._attr_unique_id = f"{self._base_id}_{self.utility_key}_reading"

    @property
    def native_value(self):
        """Return the state."""
        data = self._get_utility_data()
        if data and data.get("LastMeterReading"):
            try:
                return float(data["LastMeterReading"])
            except ValueError:
                pass
        return None


class InsiteUtilityRateSensor(InsiteUtilityEntity, SensorEntity):
    """Utility Rate Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Rate"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP/kWh"
    _attr_icon = "mdi:cash-multiple"

    def __init__(self, coordinator, utility_key, utility_name):
        """Initialize."""
        super().__init__(coordinator, utility_key, utility_name)
        self._attr_unique_id = f"{self._base_id}_{self.utility_key}_rate"

    @property
    def native_value(self):
        """Return the state."""
        data = self._get_utility_data()
        if not data:
            return None
        return parse_pence(data.get("Rates"))


class InsiteUtilityStandingChargeSensor(InsiteUtilityEntity, SensorEntity):
    """Utility Standing Charge Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Standing Charge"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP/day"
    _attr_icon = "mdi:cash-clock"

    def __init__(self, coordinator, utility_key, utility_name):
        """Initialize."""
        super().__init__(coordinator, utility_key, utility_name)
        self._attr_unique_id = (
            f"{self._base_id}_{self.utility_key}_standing_charge"
        )

    @property
    def native_value(self):
        """Return the state."""
        data = self._get_utility_data()
        if not data:
            return None
        return parse_pence(data.get("StandingChargeValue"))


class InsiteUtilityReadingDateSensor(InsiteUtilityEntity, SensorEntity):
    """Utility Last Reading Date Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Last Reading Date"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar"

    def __init__(self, coordinator, utility_key, utility_name):
        """Initialize."""
        super().__init__(coordinator, utility_key, utility_name)
        self._attr_unique_id = (
            f"{self._base_id}_{self.utility_key}_reading_date"
        )

    @property
    def native_value(self):
        """Return the state."""
        data = self._get_utility_data()
        if not data:
            return None
        return parse_reading_date(
            data.get("MeterReadingDate"), dt_util.DEFAULT_TIME_ZONE
        )


class InsiteUtilitySerialNumberSensor(InsiteUtilityEntity, SensorEntity):
    """Utility Meter Serial Number Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Meter Serial Number"
    _attr_icon = "mdi:barcode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, utility_key, utility_name):
        """Initialize."""
        super().__init__(coordinator, utility_key, utility_name)
        self._attr_unique_id = f"{self._base_id}_{self.utility_key}_serial"

    @property
    def native_value(self):
        """Return the state."""
        data = self._get_utility_data()
        if data and data.get("MeterSerialNumber"):
            return data["MeterSerialNumber"]
        return None
