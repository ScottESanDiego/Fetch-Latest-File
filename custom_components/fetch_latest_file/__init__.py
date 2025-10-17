"""The Fetch Latest File integration."""
import asyncio
import logging
import os
import time
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, IMAGE_EXTS, VIDEO_EXTS, AUDIO_EXTS

_LOGGER = logging.getLogger(__name__)

# Define platforms to set up
PLATFORMS = ["sensor"]

# Service schema for validation
SERVICE_FETCH_SCHEMA = vol.Schema({
    vol.Required("directory"): cv.string,
    vol.Required("filename"): cv.string,
    vol.Optional("extension"): vol.Any(cv.string, [cv.string]),
    vol.Optional("minsize", default="0B"): cv.string,
})

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FetchLatestFile from a config entry."""
    _LOGGER.info("Setting up FetchLatestFile integration entry_id: %s", entry.entry_id)

    # Ensure hass.data structure exists for this domain and entry
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})

    # Set up the sensor platform using the correct function
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def handle_fetch(call: ServiceCall) -> None:
        """Handle the service call to fetch the latest file(s) and update the sensor."""
        _LOGGER.debug("Service fetch_latest_file.fetch called with data: %s", call.data)

        # Retrieve the sensor entity instance stored during platform setup
        sensor_entity: Any = hass.data[DOMAIN][entry.entry_id].get('sensor_entity')
        if not sensor_entity:
            _LOGGER.error("FetchLatestFile sensor entity not found in hass.data. Cannot update state.")
            return

        # Extract validated parameters
        directory: str = call.data["directory"]
        file_name_prefix: str = call.data["filename"]
        extensions = call.data.get("extension")
        min_size_str: str = call.data.get("minsize", "0B")

        # Normalize extensions
        if extensions is None:
            extensions = []
        elif isinstance(extensions, str):
            extensions = [extensions]
        cleaned_extensions = {ext.lower().strip('.') for ext in extensions if isinstance(ext, str)}

        # Parse minimum size
        min_size = 0
        size_multiplier = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3}
        min_size_str_upper = min_size_str.upper().strip()
        try:
            unit = next((u for u in size_multiplier if min_size_str_upper.endswith(u)), "B")
            value_str = min_size_str_upper
            if unit != "B":
                value_str = value_str[:-len(unit)]
            else:
                value_str = value_str.rstrip('B')
            if not value_str:
                raise ValueError("Numeric size value missing.")
            min_size = int(value_str) * size_multiplier[unit]
            if min_size < 0:
                raise ValueError("Minimum size cannot be negative.")
        except ValueError as e:
            _LOGGER.error("Invalid 'minsize' input '%s': %s", min_size_str, e)
            sensor_entity.update_data("Error: Invalid Size", {"error_details": f"Input: {min_size_str}, Error: {e}"})
            return

        # Run blocking I/O operations in executor
        def search_files() -> list[tuple[float, str, str, str]]:
            """Search for matching files (blocking operation)."""
            # Validate directory
            if not os.path.isdir(directory):
                raise ValueError(f"Invalid or inaccessible directory: {directory}")

            _LOGGER.debug("Searching in '%s' for files starting with '%s'", directory, file_name_prefix)
            if cleaned_extensions:
                _LOGGER.debug("Filtering by extensions: %s", cleaned_extensions)
            if min_size > 0:
                _LOGGER.debug("Filtering by minimum size: %s (%d bytes)", min_size_str, min_size)

            found_files = []
            for dirpath, _, filenames in os.walk(directory):
                for filename in filenames:
                    if filename.lower().startswith(file_name_prefix.lower()):
                        file_path = os.path.join(dirpath, filename)
                        try:
                            stats = os.stat(file_path)
                            mod_time = stats.st_mtime
                            file_size = stats.st_size
                            file_ext = os.path.splitext(filename)[1].lower().strip('.')

                            extension_match = not cleaned_extensions or file_ext in cleaned_extensions
                            size_match = file_size >= min_size

                            if extension_match and size_match:
                                file_type = "generic"
                                if file_ext in IMAGE_EXTS:
                                    file_type = "image"
                                elif file_ext in VIDEO_EXTS:
                                    file_type = "video"
                                elif file_ext in AUDIO_EXTS:
                                    file_type = "audio"
                                found_files.append((mod_time, file_path, file_ext, file_type))
                                _LOGGER.debug("Found matching file: %s (Type: %s)", file_path, file_type)

                        except FileNotFoundError:
                            _LOGGER.warning("File vanished during scan: %s", file_path)
                        except OSError as e:
                            _LOGGER.warning("OS error accessing stats for %s: %s", file_path, e)
            return found_files

        try:
            found_files = await hass.async_add_executor_job(search_files)
        except ValueError as e:
            _LOGGER.error("Directory validation error: %s", e)
            sensor_entity.update_data("Error: Invalid Directory", {"error_details": str(e)})
            return
        except OSError as e:
            _LOGGER.error("OS error walking directory %s: %s", directory, e)
            sensor_entity.update_data("Error: OS Error Walking Dir", {"error_details": f"Directory: {directory}, Error: {e}"})
            return

        if not found_files:
            _LOGGER.info("No matching files found for the criteria.")
            sensor_entity.update_data("No matching files", {})
            return

        # Sort files by modification time, newest first
        found_files.sort(key=lambda x: x[0], reverse=True)

        # Get the path of the absolute latest file
        overall_latest_file_path = found_files[0][1]

        # Find the latest file for each type
        latest_by_type = {}
        processed_types = set()
        for mod_time, file_path, file_ext, file_type in found_files:
            if file_type not in processed_types:
                latest_by_type[file_type] = file_path
                processed_types.add(file_type)
            if len(processed_types) >= 4:
                break

        # Prepare state and attributes for sensor
        state_attributes = {"Overall": overall_latest_file_path}
        if 'image' in latest_by_type:
            state_attributes['Image'] = latest_by_type['image']
        if 'video' in latest_by_type:
            state_attributes['Video'] = latest_by_type['video']
        if 'audio' in latest_by_type:
            state_attributes['Audio'] = latest_by_type['audio']
        if 'generic' in latest_by_type:
            state_attributes['Generic'] = latest_by_type['generic']

        # Use timestamp as the main state for the sensor
        current_time_state = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        # Update sensor entity
        sensor_entity.update_data(state=current_time_state, attributes=state_attributes)

        _LOGGER.info("Fetch completed. Sensor '%s' updated.", sensor_entity.entity_id)
        _LOGGER.debug("Sensor state: %s, attributes: %s", current_time_state, state_attributes)

    # Register the service only once (check if it's already registered)
    if not hass.services.has_service(DOMAIN, "fetch"):
        hass.services.async_register(DOMAIN, "fetch", handle_fetch, schema=SERVICE_FETCH_SCHEMA)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading FetchLatestFile integration...")

    # Only unregister the service if this is the last entry
    if len(hass.config_entries.async_entries(DOMAIN)) == 1:
        hass.services.async_remove(DOMAIN, "fetch")

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Clean up hass.data associated with this config entry
    if unload_ok:
        if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
            hass.data[DOMAIN].pop(entry.entry_id)
            if not hass.data[DOMAIN]:
                hass.data.pop(DOMAIN)
        _LOGGER.info("FetchLatestFile integration successfully unloaded.")
    else:
        _LOGGER.error("Failed to unload one or more FetchLatestFile platforms.")

    return unload_ok

