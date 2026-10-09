"""Sensor publication for entry-owned Fetch Latest File results."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import FetchLatestFileConfigEntry
from .const import DOMAIN, INTEGRATION_TITLE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FetchLatestFileConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the publisher; disabled entities never attach to the runtime."""
    async_add_entities([FetchLatestFileSensor(entry)])


class FetchLatestFileSensor(SensorEntity):
    """Publish results without owning their storage or operation locks."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_name = None
    _attr_icon = "mdi:file-find"

    def __init__(self, entry: FetchLatestFileConfigEntry) -> None:
        """Initialize the sensor identity."""
        self._entry = entry
        self._runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_latest_file"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=INTEGRATION_TITLE,
            manufacturer="FetchLatestFile Integration",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._attr_native_value = self._runtime.last_fetch
        self._attr_extra_state_attributes = dict(self._runtime.targets)

    @property
    def state(self) -> str | None:
        """Preserve fetch precision after HA validates the timestamp value."""
        state = super().state
        value = self.native_value
        if isinstance(value, datetime):
            # HA's timestamp sensor serializer otherwise drops microseconds.
            return dt_util.as_utc(value).isoformat()
        return state

    async def async_added_to_hass(self) -> None:
        """Attach only after HA has enabled and added this entity."""
        await super().async_added_to_hass()
        self._runtime.sensor_entity = self
        self.publish_runtime()

    async def async_will_remove_from_hass(self) -> None:
        """Detach before publication could reach a removed entity."""
        if self._runtime.sensor_entity is self:
            self._runtime.sensor_entity = None
        await super().async_will_remove_from_hass()

    def publish_runtime(self) -> None:
        """Update state atomically from the runtime snapshot."""
        self._attr_native_value = self._runtime.last_fetch
        self._attr_extra_state_attributes = dict(self._runtime.targets)
        self.async_write_ha_state()
