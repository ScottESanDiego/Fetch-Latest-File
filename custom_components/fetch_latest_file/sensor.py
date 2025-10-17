"""Sensor platform for FetchLatestFile."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    sensor = FetchLatestFileSensor(entry)
    async_add_entities([sensor], True)

    # Store the sensor entity instance for the service call to access
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    if entry.entry_id not in hass.data[DOMAIN]:
        hass.data[DOMAIN][entry.entry_id] = {}
    hass.data[DOMAIN][entry.entry_id]['sensor_entity'] = sensor
    _LOGGER.info("FetchLatestFile Sensor setup complete.")


class FetchLatestFileSensor(SensorEntity):
    """Representation of the FetchLatestFile Sensor."""

    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_latest_file"
        self._attr_name = "Fetch Latest File"
        self._attr_icon = "mdi:file-find"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Fetch Latest File",
            manufacturer="FetchLatestFile Integration",
            entry_type="service",
        )

        self._attr_native_value: StateType = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    def update_data(self, state: StateType, attributes: dict[str, Any]) -> None:
        """Update sensor state and attributes, then trigger HA state update."""
        _LOGGER.debug("Updating FetchLatestFile sensor state to: %s, attributes: %s", state, attributes)
        self._attr_native_value = state
        self._attr_extra_state_attributes = attributes
        self.async_schedule_update_ha_state(force_refresh=False)


