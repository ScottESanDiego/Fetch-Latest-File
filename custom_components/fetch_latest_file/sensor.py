"""Sensor platform for FetchLatestFile."""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FetchLatestFileConfigEntry
from .const import (
    CONF_MAX_TARGET_IDS,
    CONF_TARGET_EXPIRY_HOURS,
    DOMAIN,
    DEFAULT_MAX_TARGET_IDS,
    DEFAULT_TARGET_EXPIRY_HOURS,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: FetchLatestFileConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    options = entry.options
    max_target_ids = options.get(CONF_MAX_TARGET_IDS, DEFAULT_MAX_TARGET_IDS)
    target_expiry_hours = options.get(CONF_TARGET_EXPIRY_HOURS, DEFAULT_TARGET_EXPIRY_HOURS)
    
    sensor = FetchLatestFileSensor(entry, max_target_ids, target_expiry_hours)
    entry.runtime_data.sensor_entity = sensor
    async_add_entities([sensor], True)

    _LOGGER.info("FetchLatestFile Sensor setup complete.")


class FetchLatestFileSensor(SensorEntity):
    """Representation of the FetchLatestFile Sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        entry: FetchLatestFileConfigEntry,
        max_target_ids: int,
        target_expiry_hours: int,
    ) -> None:
        """Initialize the sensor."""
        self._entry = entry
        self._max_target_ids = max_target_ids
        self._target_expiry_seconds = target_expiry_hours * 3600  # Convert hours to seconds
        
        self._attr_unique_id = f"{entry.entry_id}_latest_file"
        self._attr_name = None
        self._attr_icon = "mdi:file-find"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Fetch Latest File",
            manufacturer="FetchLatestFile Integration",
            entry_type=DeviceEntryType.SERVICE,
        )

        self._attr_native_value: datetime | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}
        
        # Dictionary of locks per target_id for parallel execution
        self._locks: dict[str, asyncio.Lock] = {}

    def get_lock(self, target_id: str) -> asyncio.Lock:
        """Get or create a lock for the given target_id."""
        if target_id not in self._locks:
            # Note: Creating locks is not async, so this is safe
            self._locks[target_id] = asyncio.Lock()
        return self._locks[target_id]

    def update_target_data(self, target_id: str, data: dict[str, Any]) -> None:
        """Update sensor with data namespaced by target_id."""
        _LOGGER.debug("Updating FetchLatestFile sensor for target_id '%s': %s", target_id, data)
        
        # Get or initialize the targets dictionary in attributes
        if not isinstance(self._attr_extra_state_attributes, dict):
            self._attr_extra_state_attributes = {}
        
        # Store data under the target_id key
        self._attr_extra_state_attributes[target_id] = data
        
        # Update main state to most recent update timestamp
        if "timestamp" in data:
            self._attr_native_value = data["timestamp"]
        
        # Cleanup old/stale target_ids
        self._cleanup_old_targets()
        
        self.async_write_ha_state()

    @staticmethod
    def _timestamp_epoch(value: Any) -> float | None:
        """Return epoch seconds for timestamp values."""
        if isinstance(value, datetime):
            return value.timestamp()

        return None

    def _cleanup_old_targets(self) -> None:
        """Remove old or excess target_ids to prevent unbounded growth."""
        if not isinstance(self._attr_extra_state_attributes, dict):
            return
        
        import time as time_module
        current_time = time_module.time()
        targets_to_remove = []
        
        # Target IDs have dict values with a timestamp key.
        target_ids = {
            k for k, v in self._attr_extra_state_attributes.items()
            if isinstance(v, dict) and "timestamp" in v
        }
        
        # First, remove expired targets (older than configured expiry time)
        for target_id in target_ids:
            data = self._attr_extra_state_attributes[target_id]
            if isinstance(data, dict) and "timestamp" in data:
                target_time = self._timestamp_epoch(data["timestamp"])
                if target_time is None:
                    _LOGGER.warning("Could not parse timestamp for target '%s'", target_id)
                    continue

                if current_time - target_time > self._target_expiry_seconds:
                    targets_to_remove.append(target_id)
                    _LOGGER.debug(
                        "Target '%s' expired (age: %d seconds)",
                        target_id,
                        int(current_time - target_time),
                    )
        
        for target_id in targets_to_remove:
            del self._attr_extra_state_attributes[target_id]
        
        # If still over limit, remove oldest targets
        # Recalculate remaining target_ids after removing expired ones
        remaining_target_ids = {
            k: v for k, v in self._attr_extra_state_attributes.items()
            if isinstance(v, dict) and "timestamp" in v
        }
        
        if len(remaining_target_ids) > self._max_target_ids:
            # Sort by timestamp to find oldest
            target_times = []
            for target_id in remaining_target_ids:
                data = self._attr_extra_state_attributes[target_id]
                if isinstance(data, dict) and "timestamp" in data:
                    target_times.append((target_id, self._timestamp_epoch(data["timestamp"]) or 0))
                else:
                    # No timestamp, consider it very old
                    target_times.append((target_id, 0))
            
            # Sort by time (oldest first)
            target_times.sort(key=lambda x: x[1])
            
            # Remove oldest targets until we're at the limit
            num_to_remove = len(remaining_target_ids) - self._max_target_ids
            for i in range(num_to_remove):
                target_id_to_remove = target_times[i][0]
                _LOGGER.debug(
                    "Removing oldest target '%s' to maintain limit of %d",
                    target_id_to_remove,
                    self._max_target_ids,
                )
                del self._attr_extra_state_attributes[target_id_to_remove]
        
        if targets_to_remove or len(remaining_target_ids) > self._max_target_ids:
            _LOGGER.info(
                "Cleaned up %d target_id(s), %d remaining",
                len(targets_to_remove),
                len(remaining_target_ids),
            )
