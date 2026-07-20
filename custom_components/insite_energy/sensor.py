"""Sensor platform for Insite Energy."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import InsiteEnergyDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Insite Energy sensor platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    # Account Device Sensors
    entities.append(InsiteAccountBalanceSensor(coordinator))
    entities.append(InsiteAccountLastPollSensor(coordinator))

    # Utility Devices Sensors
    # NOTE: Entities are created from the initial data snapshot. If the API
    # later returns new utilities, they won't appear until HA is restarted.
    view_model = coordinator.data
    if view_model and "UtilityDetails" in view_model:
        for utility in view_model["UtilityDetails"]:
            if "Name" in utility:
                name = utility["Name"]
                entities.append(InsiteUtilityReadingSensor(coordinator, name))
                entities.append(InsiteUtilityRateSensor(coordinator, name))
                entities.append(InsiteUtilityStandingChargeSensor(coordinator, name))
                entities.append(InsiteUtilityReadingDateSensor(coordinator, name))
                entities.append(InsiteUtilitySerialNumberSensor(coordinator, name))

    async_add_entities(entities)


class InsiteEnergyBaseEntity(CoordinatorEntity):
    """Base entity for Insite Energy."""

    def __init__(self, coordinator: InsiteEnergyDataUpdateCoordinator) -> None:
        """Initialize the base entity."""
        super().__init__(coordinator)


class InsiteAccountEntity(InsiteEnergyBaseEntity):
    """Base entity for the Account Details device."""

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.username}_account")},
            "name": "Account Details",
            "manufacturer": "Insite Energy",
        }


class InsiteUtilityEntity(InsiteEnergyBaseEntity):
    """Base entity for Utility devices."""

    def __init__(
        self, coordinator: InsiteEnergyDataUpdateCoordinator, utility_name: str
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.utility_name = utility_name
        self._safe_name = (
            utility_name.replace(" & ", "_").replace(" ", "_").lower()
        )

    @property
    def device_info(self):
        """Return device information."""
        serial = ""
        data = self._get_utility_data()
        if data and data.get("MeterSerialNumber"):
            serial = f" ({data.get('MeterSerialNumber')})"

        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.username}_{self.utility_name}")},
            "name": f"{self.utility_name}{serial}",
            "manufacturer": "Insite Energy",
            "model": "Utility Meter",
        }

    def _get_utility_data(self):
        """Helper to find the specific utility dict."""
        if not self.coordinator.data or "UtilityDetails" not in self.coordinator.data:
            return None

        for utility in self.coordinator.data["UtilityDetails"]:
            if utility.get("Name") == self.utility_name:
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
        self._attr_unique_id = f"{coordinator.username}_account_balance"
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
        self._attr_unique_id = f"{coordinator.username}_account_last_poll"

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

    def __init__(self, coordinator, utility_name):
        """Initialize."""
        super().__init__(coordinator, utility_name)
        self._attr_unique_id = f"{coordinator.username}_{self._safe_name}_reading"

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
    _attr_icon = "mdi:cash-multiple"

    def __init__(self, coordinator, utility_name):
        """Initialize."""
        super().__init__(coordinator, utility_name)
        self._attr_unique_id = f"{coordinator.username}_{self._safe_name}_rate"

    @property
    def native_value(self):
        """Return the state."""
        data = self._get_utility_data()
        if data and data.get("Rates"):
            return data["Rates"]
        return None


class InsiteUtilityStandingChargeSensor(InsiteUtilityEntity, SensorEntity):
    """Utility Standing Charge Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Standing Charge"
    _attr_icon = "mdi:cash-clock"

    def __init__(self, coordinator, utility_name):
        """Initialize."""
        super().__init__(coordinator, utility_name)
        self._attr_unique_id = (
            f"{coordinator.username}_{self._safe_name}_standing_charge"
        )

    @property
    def native_value(self):
        """Return the state."""
        data = self._get_utility_data()
        if data and data.get("StandingChargeValue"):
            return data["StandingChargeValue"]
        return None


class InsiteUtilityReadingDateSensor(InsiteUtilityEntity, SensorEntity):
    """Utility Last Reading Date Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Last Reading Date"
    _attr_icon = "mdi:calendar"

    def __init__(self, coordinator, utility_name):
        """Initialize."""
        super().__init__(coordinator, utility_name)
        self._attr_unique_id = (
            f"{coordinator.username}_{self._safe_name}_reading_date"
        )

    @property
    def native_value(self):
        """Return the state."""
        data = self._get_utility_data()
        if data and data.get("MeterReadingDate"):
            return data["MeterReadingDate"]
        return None


class InsiteUtilitySerialNumberSensor(InsiteUtilityEntity, SensorEntity):
    """Utility Meter Serial Number Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Meter Serial Number"
    _attr_icon = "mdi:barcode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, utility_name):
        """Initialize."""
        super().__init__(coordinator, utility_name)
        self._attr_unique_id = f"{coordinator.username}_{self._safe_name}_serial"

    @property
    def native_value(self):
        """Return the state."""
        data = self._get_utility_data()
        if data and data.get("MeterSerialNumber"):
            return data["MeterSerialNumber"]
        return None
