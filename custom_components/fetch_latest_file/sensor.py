"""Sensor platform for FetchLatestFile."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN,
    DEFAULT_MAX_TARGET_IDS as MAX_TARGET_IDS,
    DEFAULT_TARGET_EXPIRY_HOURS,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    # Get configuration options
    options = entry.options
    max_target_ids = options.get("max_target_ids", MAX_TARGET_IDS)
    target_expiry_hours = options.get("target_expiry_hours", 24)
    
    sensor = FetchLatestFileSensor(entry, max_target_ids, target_expiry_hours)
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

    def __init__(self, entry: ConfigEntry, max_target_ids: int, target_expiry_hours: int) -> None:
        """Initialize the sensor."""
        self._entry = entry
        self._max_target_ids = max_target_ids
        self._target_expiry_seconds = target_expiry_hours * 3600  # Convert hours to seconds
        
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
        
        # Dictionary of locks per target_id for parallel execution
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()  # Lock for accessing the locks dict

    def get_lock(self, target_id: str) -> asyncio.Lock:
        """Get or create a lock for the given target_id."""
        if target_id not in self._locks:
            # Note: Creating locks is not async, so this is safe
            # The locks_lock is only needed if we were doing async operations
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
        
        # For backward compatibility: if target_id is 'default', also flatten to top level
        if target_id == "default":
            # Add all keys from default to top level (except 'timestamp' to avoid confusion)
            for key, value in data.items():
                if key != "timestamp":  # Don't duplicate timestamp at top level
                    self._attr_extra_state_attributes[key] = value
        
        # Update main state to most recent update timestamp
        if "timestamp" in data:
            self._attr_native_value = data["timestamp"]
        
        # Cleanup old/stale target_ids
        self._cleanup_old_targets()
        
        self.async_schedule_update_ha_state(force_refresh=False)

    def _cleanup_old_targets(self) -> None:
        """Remove old or excess target_ids to prevent unbounded growth."""
        if not isinstance(self._attr_extra_state_attributes, dict):
            return
        
        import time as time_module
        current_time = time_module.time()
        targets_to_remove = []
        
        # Identify which keys are target_ids (vs top-level backward compatibility keys)
        # Target_ids have dict values with 'timestamp' key
        target_ids = {
            k for k, v in self._attr_extra_state_attributes.items()
            if isinstance(v, dict) and "timestamp" in v
        }
        
        # First, remove expired targets (older than configured expiry time)
        for target_id in target_ids:
            data = self._attr_extra_state_attributes[target_id]
            if isinstance(data, dict) and "timestamp" in data:
                try:
                    # Parse timestamp string to epoch time for comparison
                    from datetime import datetime
                    timestamp_str = data["timestamp"]
                    # Parse ISO format timestamp (e.g., "2025-10-26T14:30:45-0700")
                    dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                    target_time = dt.timestamp()
                    
                    if current_time - target_time > self._target_expiry_seconds:
                        targets_to_remove.append(target_id)
                        _LOGGER.debug("Target '%s' expired (age: %d seconds)", target_id, int(current_time - target_time))
                except (ValueError, AttributeError) as e:
                    _LOGGER.warning("Could not parse timestamp for target '%s': %s", target_id, e)
        
        # Remove expired targets (and their top-level keys if target is 'default')
        for target_id in targets_to_remove:
            if target_id == "default":
                # Also remove top-level backward compatibility keys
                default_data = self._attr_extra_state_attributes.get("default", {})
                if isinstance(default_data, dict):
                    for key in list(default_data.keys()):
                        if key != "timestamp" and key in self._attr_extra_state_attributes:
                            # Only remove if it matches the default value (not overwritten by another target)
                            if self._attr_extra_state_attributes.get(key) == default_data.get(key):
                                del self._attr_extra_state_attributes[key]
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
                    try:
                        from datetime import datetime
                        timestamp_str = data["timestamp"]
                        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                        target_times.append((target_id, dt.timestamp()))
                    except (ValueError, AttributeError):
                        # If we can't parse, consider it very old
                        target_times.append((target_id, 0))
                else:
                    # No timestamp, consider it very old
                    target_times.append((target_id, 0))
            
            # Sort by time (oldest first)
            target_times.sort(key=lambda x: x[1])
            
            # Remove oldest targets until we're at the limit
            num_to_remove = len(remaining_target_ids) - self._max_target_ids
            for i in range(num_to_remove):
                target_id_to_remove = target_times[i][0]
                _LOGGER.debug("Removing oldest target '%s' to maintain limit of %d", target_id_to_remove, self._max_target_ids)
                
                # Remove top-level keys if this is 'default'
                if target_id_to_remove == "default":
                    default_data = self._attr_extra_state_attributes.get("default", {})
                    if isinstance(default_data, dict):
                        for key in list(default_data.keys()):
                            if key != "timestamp" and key in self._attr_extra_state_attributes:
                                if self._attr_extra_state_attributes.get(key) == default_data.get(key):
                                    del self._attr_extra_state_attributes[key]
                
                del self._attr_extra_state_attributes[target_id_to_remove]
        
        if targets_to_remove or len(remaining_target_ids) > self._max_target_ids:
            _LOGGER.info("Cleaned up %d target_id(s), %d remaining", 
                        len(targets_to_remove), 
                        len(remaining_target_ids))

    def update_data(self, state: StateType, attributes: dict[str, Any]) -> None:
        """Update sensor state and attributes (legacy method for backward compatibility)."""
        _LOGGER.debug("Updating FetchLatestFile sensor state to: %s, attributes: %s", state, attributes)
        self._attr_native_value = state
        # For backward compatibility, store under 'default' target_id
        self._attr_extra_state_attributes = {"default": attributes}
        self.async_schedule_update_ha_state(force_refresh=False)


