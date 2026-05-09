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

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Define platforms to set up
PLATFORMS = ["sensor"]

RESERVED_RESULT_KEYS = {"Overall", "timestamp", "status", "error", "error_details", "default"}

# Service schema for validation
SERVICE_FETCH_SCHEMA = vol.Schema({
    vol.Required("directory"): cv.string,
    vol.Required("filename"): cv.string,
    vol.Optional("extension"): vol.Any(cv.string, [cv.string]),
    vol.Optional("min_size", default="0B"): cv.string,
    vol.Optional("target_id"): cv.string,
})

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FetchLatestFile from a config entry."""
    _LOGGER.info("Setting up FetchLatestFile integration entry_id: %s", entry.entry_id)

    # Ensure hass.data structure exists for this domain and entry
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})

    # Set up the sensor platform using the correct function
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register options update listener
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    async def handle_fetch(call: ServiceCall) -> None:
        """Handle the service call to fetch the latest file(s) and update the sensor."""
        _LOGGER.debug("Service fetch_latest_file.fetch called with data: %s", call.data)

        # Use default sensor entity (backward compatibility)
        sensor_entity: Any = hass.data[DOMAIN][entry.entry_id].get('sensor_entity')
        if not sensor_entity:
            _LOGGER.error("FetchLatestFile sensor entity not found in hass.data. Cannot update state.")
            return
        
        # Get target_id for lock management (defaults to 'default')
        target_id = call.data.get("target_id", "default")
        
        # Acquire lock based on target_id to allow parallel execution with different IDs
        async with sensor_entity.get_lock(target_id):

            # Extract validated parameters
            directory: str = call.data["directory"]
            file_name_prefix: str = call.data["filename"]
            extensions = call.data.get("extension")
            min_size_str: str = call.data.get("min_size", "0B")

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
                _LOGGER.error("Invalid 'min_size' input '%s': %s", min_size_str, e)
                sensor_entity.update_target_data(target_id, {
                    "error": "Invalid Size",
                    "error_details": f"Input: {min_size_str}, Error: {e}",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")
                })
                return

            # Run blocking I/O operations in executor
            def search_files() -> list[tuple[float, str, str]]:
                """Search for matching files (blocking operation)."""
                # Security: Resolve to absolute path to prevent directory traversal
                try:
                    real_directory = os.path.realpath(directory)
                except OSError as e:
                    raise ValueError(f"Cannot resolve directory path: {directory}, Error: {e}")
                
                # Verify the resolved path still exists and is a directory
                if not os.path.isdir(real_directory):
                    raise ValueError(f"Invalid or inaccessible directory: {directory}")
                
                # Security: Resolve to absolute path to prevent directory traversal
                try:
                    real_directory = os.path.realpath(directory)
                except OSError as e:
                    raise ValueError(f"Cannot resolve directory path: {directory}, Error: {e}")
                
                # Verify the resolved path still exists and is a directory
                if not os.path.isdir(real_directory):
                    raise ValueError(f"Resolved path is not a directory: {real_directory}")
                
                # Check read permissions
                if not os.access(real_directory, os.R_OK):
                    raise ValueError(f"No read permission for directory: {real_directory}")

                # Security: Sanitize filename prefix to prevent path traversal patterns
                if file_name_prefix and ('/' in file_name_prefix or '\\' in file_name_prefix or '..' in file_name_prefix):
                    raise ValueError(f"Invalid filename prefix contains path separators or '..' : {file_name_prefix}")
                
                _LOGGER.debug("Searching in '%s' for files starting with '%s'", real_directory, file_name_prefix)
                if cleaned_extensions:
                    _LOGGER.debug("Filtering by extensions: %s", cleaned_extensions)
                if min_size > 0:
                    _LOGGER.debug("Filtering by minimum size: %s (%d bytes)", min_size_str, min_size)

                found_files = []
                max_depth = 10  # Limit recursion depth to prevent DoS
                file_count = 0
                max_files_to_check = 10000  # Limit number of files checked to prevent DoS
                
                for dirpath, dirnames, filenames in os.walk(real_directory, followlinks=False):  # Don't follow symlinks
                    # Calculate current depth
                    depth = dirpath[len(real_directory):].count(os.sep)
                    if depth > max_depth:
                        _LOGGER.debug("Skipping directory (too deep): %s", dirpath)
                        dirnames.clear()  # Don't recurse deeper
                        continue
                    
                    # Security: Ensure we're still within the allowed directory
                    try:
                        real_dirpath = os.path.realpath(dirpath)
                        if not real_dirpath.startswith(real_directory):
                            _LOGGER.warning("Skipping directory outside base path: %s", dirpath)
                            continue
                    except OSError:
                        _LOGGER.warning("Could not resolve path: %s", dirpath)
                        continue
                    
                    for filename in filenames:
                        file_count += 1
                        if file_count > max_files_to_check:
                            _LOGGER.warning("Reached maximum file check limit (%d), stopping search", max_files_to_check)
                            return found_files
                        
                        if filename.lower().startswith(file_name_prefix.lower()):
                            file_path = os.path.join(dirpath, filename)
                            
                            # Security: Validate file path is still within base directory
                            try:
                                real_file_path = os.path.realpath(file_path)
                                if not real_file_path.startswith(real_directory):
                                    _LOGGER.warning("Skipping file outside base directory: %s", file_path)
                                    continue
                            except OSError:
                                _LOGGER.warning("Could not resolve file path: %s", file_path)
                                continue
                            
                            try:
                                stats = os.stat(file_path, follow_symlinks=False)  # Don't follow symlinks
                                
                                # Skip if it's a symlink (extra safety)
                                if os.path.islink(file_path):
                                    _LOGGER.debug("Skipping symlink: %s", file_path)
                                    continue
                                
                                mod_time = stats.st_mtime
                                file_size = stats.st_size
                                file_ext = os.path.splitext(filename)[1].lower().strip('.')

                                extension_match = not cleaned_extensions or file_ext in cleaned_extensions
                                size_match = file_size >= min_size

                                if extension_match and size_match:
                                    found_files.append((mod_time, file_path, file_ext))
                                    _LOGGER.debug("Found matching file: %s (Extension: %s)", file_path, file_ext or "no_extension")

                            except FileNotFoundError:
                                _LOGGER.warning("File vanished during scan: %s", file_path)
                            except OSError as e:
                                _LOGGER.warning("OS error accessing stats for %s: %s", file_path, e)
                return found_files

            try:
                found_files = await hass.async_add_executor_job(search_files)
            except ValueError as e:
                _LOGGER.error("Directory validation error: %s", e)
                sensor_entity.update_target_data(target_id, {
                    "error": "Invalid Directory",
                    "error_details": str(e),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")
                })
                return
            except OSError as e:
                _LOGGER.error("OS error walking directory %s: %s", directory, e)
                sensor_entity.update_target_data(target_id, {
                    "error": "OS Error Walking Dir",
                    "error_details": f"Directory: {directory}, Error: {e}",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")
                })
                return

            if not found_files:
                _LOGGER.info("No matching files found for the criteria.")
                sensor_entity.update_target_data(target_id, {
                    "status": "No matching files",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")
                })
                return

            # Sort files by modification time, newest first
            found_files.sort(key=lambda x: x[0], reverse=True)

            # Get the path of the absolute latest file
            overall_latest_file_path = found_files[0][1]

            # Find the latest file for each extension
            latest_by_extension = {}
            for mod_time, file_path, file_ext in found_files:
                extension_key = file_ext or "no_extension"
                if extension_key in RESERVED_RESULT_KEYS:
                    extension_key = f"ext_{extension_key}"
                if extension_key not in latest_by_extension:
                    latest_by_extension[extension_key] = file_path

            # Prepare state and attributes for sensor
            file_results = {
                "Overall": overall_latest_file_path,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")
            }
            file_results.update(latest_by_extension)

            # Update sensor entity with target_id namespaced results
            sensor_entity.update_target_data(target_id, file_results)

            _LOGGER.info("Fetch completed for target_id '%s'. Sensor '%s' updated.", target_id, sensor_entity.entity_id)
            _LOGGER.debug("Target '%s' results: %s", target_id, file_results)

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


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
