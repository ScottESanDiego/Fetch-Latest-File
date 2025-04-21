"""Sensor platform for FetchLatestFile."""
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.entity import DeviceInfo # Optional: for device linking

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    # Create the sensor entity
    sensor = FetchLatestFileSensor(entry)
    async_add_entities([sensor], True) # True = update before adding

    # Store the sensor entity instance in hass.data for the service call to access
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    if entry.entry_id not in hass.data[DOMAIN]:
        hass.data[DOMAIN][entry.entry_id] = {}
    hass.data[DOMAIN][entry.entry_id]['sensor_entity'] = sensor
    _LOGGER.info("FetchLatestFile Sensor setup complete.")


class FetchLatestFileSensor(SensorEntity):
    """Representation of the FetchLatestFile Sensor."""

    # Set _attr_should_poll to False because updates are pushed from the service call
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        self._entry = entry
        # Unique ID for the sensor entity in the registry
        # Since it's a single config entry, we can use a fixed suffix
        self._attr_unique_id = f"{entry.entry_id}_latest_file"
        # Entity ID will be sensor.flf_latest_file (derived from name)
        self._attr_name = "Fetch Latest File"
        self._attr_icon = "mdi:file-find"

        # Optional: Link sensor to a device for better organization in HA
        # The device identifier uses the config entry ID to ensure uniqueness
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Fetch Latest File", # Name of the device grouping
            manufacturer="FetchLatestFile Integration", # Or your name/handle
            entry_type="service", # Or remove if not applicable
        )

        # Initialize state and attributes
        self._attr_native_value: StateType = None # State (e.g., timestamp or status)
        self._attr_extra_state_attributes: dict[str, any] = {} # Attributes like Overall, Image, Video paths

    def update_data(self, state: StateType, attributes: dict[str, any]) -> None:
        """Update sensor state and attributes, then trigger HA state update."""
        _LOGGER.debug(f"Updating FetchLatestFile sensor state to: {state}, attributes: {attributes}")
        self._attr_native_value = state
        self._attr_extra_state_attributes = attributes
        # Tell HA that the state has changed and needs to be written
        self.async_schedule_update_ha_state(force_refresh=False) # False avoids immediate write if throttled


