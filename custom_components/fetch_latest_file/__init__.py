"""The Fetch Latest File integration."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import logging
import re
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ALLOWED_DIRECTORIES,
    CONF_MAX_FILES_TO_CHECK,
    CONF_MAX_SCAN_DEPTH,
    DEFAULT_ALLOWED_DIRECTORIES,
    DEFAULT_MAX_FILES_TO_CHECK,
    DEFAULT_MAX_SCAN_DEPTH,
    DOMAIN,
)
from .scanner import (
    FileSearchError,
    SearchCriteria,
    build_file_results,
    normalize_allowed_directories,
    normalize_extensions,
    parse_min_size,
    search_files,
)

if TYPE_CHECKING:
    from .sensor import FetchLatestFileSensor

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
SERVICE_FETCH = "fetch"

DEFAULT_TARGET_ID = "default"
TARGET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class FetchLatestFileRuntimeData:
    """Runtime data for Fetch Latest File."""

    sensor_entity: FetchLatestFileSensor | None = None


type FetchLatestFileConfigEntry = ConfigEntry[FetchLatestFileRuntimeData]


def _timestamp() -> datetime:
    """Return a Home Assistant-local timestamp."""
    return dt_util.now().replace(microsecond=0)


def _validate_target_id(value: str) -> str:
    """Validate target IDs used as sensor attribute keys."""
    if not TARGET_ID_PATTERN.fullmatch(value):
        raise vol.Invalid(
            "target_id must be 1-64 characters: letters, numbers, underscore, or hyphen"
        )

    return value


def _int_option(options: Mapping[str, object], key: str, default: int) -> int:
    """Return an integer option value with a safe fallback."""
    value = options.get(key, default)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default

    return default


# Service schema for validation
SERVICE_FETCH_SCHEMA = vol.Schema({
    vol.Required("directory"): cv.string,
    vol.Required("filename"): cv.string,
    vol.Optional("extension"): vol.Any(cv.string, [cv.string]),
    vol.Optional("min_size", default="0B"): cv.string,
    vol.Optional("target_id", default=DEFAULT_TARGET_ID): vol.All(
        cv.string,
        _validate_target_id,
    ),
})

async def async_setup_entry(hass: HomeAssistant, entry: FetchLatestFileConfigEntry) -> bool:
    """Set up FetchLatestFile from a config entry."""
    _LOGGER.info("Setting up FetchLatestFile integration entry_id: %s", entry.entry_id)

    entry.runtime_data = FetchLatestFileRuntimeData()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def handle_fetch(call: ServiceCall) -> None:
        """Handle the service call to fetch the latest file(s) and update the sensor."""
        _LOGGER.debug("Service fetch_latest_file.fetch called with data: %s", call.data)

        sensor_entity = entry.runtime_data.sensor_entity
        if not sensor_entity:
            _LOGGER.error("FetchLatestFile sensor entity not found. Cannot update state.")
            return

        target_id: str = call.data["target_id"]

        # Acquire lock based on target_id to allow parallel execution with different IDs
        async with sensor_entity.get_lock(target_id):
            directory: str = call.data["directory"]
            file_name_prefix: str = call.data["filename"]
            extensions = call.data.get("extension")
            min_size_str: str = call.data.get("min_size", "0B")

            try:
                min_size = parse_min_size(min_size_str)
            except ValueError as e:
                _LOGGER.error("Invalid 'min_size' input '%s': %s", min_size_str, e)
                sensor_entity.update_target_data(target_id, {
                    "error": "Invalid Size",
                    "error_details": f"Input: {min_size_str}, Error: {e}",
                    "timestamp": _timestamp(),
                })
                return

            options = entry.options
            criteria = SearchCriteria(
                directory=directory,
                filename_prefix=file_name_prefix,
                extensions=normalize_extensions(extensions),
                min_size=min_size,
                allowed_directories=normalize_allowed_directories(
                    options.get(CONF_ALLOWED_DIRECTORIES, DEFAULT_ALLOWED_DIRECTORIES)
                ),
                max_depth=_int_option(options, CONF_MAX_SCAN_DEPTH, DEFAULT_MAX_SCAN_DEPTH),
                max_files_to_check=_int_option(
                    options,
                    CONF_MAX_FILES_TO_CHECK,
                    DEFAULT_MAX_FILES_TO_CHECK,
                ),
            )

            try:
                found_files = await hass.async_add_executor_job(search_files, criteria)
            except FileSearchError as e:
                _LOGGER.error("File search validation error: %s", e)
                sensor_entity.update_target_data(target_id, {
                    "error": "Invalid Search",
                    "error_details": str(e),
                    "timestamp": _timestamp(),
                })
                return
            except OSError as e:
                _LOGGER.error("OS error walking directory %s: %s", directory, e)
                sensor_entity.update_target_data(target_id, {
                    "error": "OS Error Walking Dir",
                    "error_details": f"Directory: {directory}, Error: {e}",
                    "timestamp": _timestamp(),
                })
                return

            if not found_files:
                _LOGGER.info("No matching files found for the criteria.")
                sensor_entity.update_target_data(target_id, {
                    "status": "No matching files",
                    "timestamp": _timestamp(),
                })
                return

            file_results: dict[str, Any] = build_file_results(found_files)
            file_results["timestamp"] = _timestamp()

            # Update sensor entity with target_id namespaced results
            sensor_entity.update_target_data(target_id, file_results)

            _LOGGER.info(
                "Fetch completed for target_id '%s'. Sensor '%s' updated.",
                target_id,
                sensor_entity.entity_id,
            )
            _LOGGER.debug("Target '%s' results: %s", target_id, file_results)

    # Register the service only once (check if it's already registered)
    if not hass.services.has_service(DOMAIN, SERVICE_FETCH):
        hass.services.async_register(
            DOMAIN,
            SERVICE_FETCH,
            handle_fetch,
            schema=SERVICE_FETCH_SCHEMA,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: FetchLatestFileConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading FetchLatestFile integration...")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry.runtime_data.sensor_entity = None
        if not hass.config_entries.async_loaded_entries(DOMAIN):
            hass.services.async_remove(DOMAIN, SERVICE_FETCH)
        _LOGGER.info("FetchLatestFile integration successfully unloaded.")
    else:
        _LOGGER.error("Failed to unload one or more FetchLatestFile platforms.")

    return unload_ok
