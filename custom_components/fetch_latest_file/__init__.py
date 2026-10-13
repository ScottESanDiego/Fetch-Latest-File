"""The Fetch Latest File integration."""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime
from typing import cast

import probatio as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryError,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
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
from .runtime import (
    FetchLatestFileRuntimeData,
    int_option,
    validate_allowed_roots,
    validate_options,
)
from .scanner import (
    FileSearchError,
    SearchCriteria,
    build_file_results,
    normalize_allowed_directories,
    normalize_extensions,
    parse_min_size,
)
from .types import FetchResponse, ReasonCode, ResponseResult, TargetResult
from .upload_wait import async_fetch_files

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
SERVICE_FETCH = "fetch"

DEFAULT_TARGET_ID = "default"
TARGET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


type FetchLatestFileConfigEntry = ConfigEntry[FetchLatestFileRuntimeData]


def _timestamp() -> datetime:
    """Return a Home Assistant-local timestamp."""
    return dt_util.now()


def _validate_target_id(value: str) -> str:
    """Validate target IDs used as sensor attribute keys."""
    if not TARGET_ID_PATTERN.fullmatch(value):
        raise vol.Invalid(
            "target_id must be 1-64 characters: letters, numbers, underscore, or hyphen"
        )

    return value


def _positive_seconds(value: object) -> float:
    """Reject zero, nonfinite, and negative waiting durations."""
    try:
        seconds = float(value)
    except (ValueError, TypeError) as err:
        raise vol.Invalid("Duration must be a positive number of seconds") from err
    if not math.isfinite(seconds) or seconds <= 0:
        raise vol.Invalid("Duration must be a positive finite number of seconds")
    return seconds


def _validate_fresh_extensions(data: dict) -> dict:
    """Require fresh extensions to be part of an explicit extension filter."""
    fresh = normalize_extensions(data.get("fresh_extensions", []))
    requested = normalize_extensions(data.get("extension"))
    if not fresh <= requested:
        raise vol.Invalid("fresh_extensions must be included in extension")
    data["fresh_extensions"] = sorted(fresh)
    return data


class _FetchSchema(vol.Schema):
    """Keep service-field metadata available while checking cross-field input."""

    def __call__(self, data):
        return _validate_fresh_extensions(super().__call__(data))


# Service schema for validation
SERVICE_FETCH_SCHEMA = _FetchSchema(
    {
        vol.Required("directory"): cv.string,
        vol.Required("filename"): cv.string,
        vol.Optional("extension"): vol.Any(cv.string, [cv.string]),
        vol.Optional("min_size", default="0B"): cv.string,
        vol.Optional("wait_for_upload", default=False): cv.boolean,
        vol.Optional("fresh_extensions", default=[]): [cv.string],
        vol.Optional("freshness_window", default=5): _positive_seconds,
        vol.Optional("timeout", default=30): _positive_seconds,
        vol.Optional("quiet_period", default=2): _positive_seconds,
        vol.Optional("target_id", default=DEFAULT_TARGET_ID): vol.All(
            cv.string,
            _validate_target_id,
        ),
    }
)


def _response_value(result: TargetResult) -> ResponseResult:
    """Return an independent snapshot with a serialized native timestamp."""
    status = result["FetchStatus"]
    return cast(
        ResponseResult,
        {
            **result,
            "timestamp": result["timestamp"].isoformat(),
            "FetchStatus": {
                "status": status["status"],
                "reasons": {
                    key: list(reasons) for key, reasons in status["reasons"].items()
                },
            },
        },
    )


def _publish_error(
    runtime: FetchLatestFileRuntimeData,
    target_id: str,
    reason: ReasonCode,
    title: str,
    details: str,
) -> None:
    """Clear previous paths before surfacing an action error."""
    runtime.publish(
        target_id,
        {
            "timestamp": _timestamp(),
            "FetchStatus": {"status": "error", "reasons": {"Overall": [reason]}},
            "error": title,
            "error_details": details,
        },
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the persistent action without capturing a config entry."""

    async def handle_fetch(call: ServiceCall) -> ServiceResponse:
        invocation_time_ns = time.time_ns()
        runtime: FetchLatestFileRuntimeData | None = hass.data.get(DOMAIN)
        if runtime is None or runtime.unloading:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="unavailable"
            )
        target_id: str = call.data["target_id"]
        async with runtime.operation(target_id):
            if runtime.unloading:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="unavailable"
                )
            directory: str = call.data["directory"]
            min_size_str: str = call.data["min_size"]
            try:
                min_size = parse_min_size(min_size_str)
            except ValueError as err:
                _publish_error(
                    runtime, target_id, "invalid_size", "Invalid Size", str(err)
                )
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="invalid_size",
                    translation_placeholders={
                        "min_size": min_size_str,
                        "error": str(err),
                    },
                ) from err

            options = runtime.options
            criteria = SearchCriteria(
                directory=directory,
                filename_prefix=call.data["filename"],
                extensions=normalize_extensions(call.data.get("extension")),
                min_size=min_size,
                allowed_directories=normalize_allowed_directories(
                    options.get(CONF_ALLOWED_DIRECTORIES, DEFAULT_ALLOWED_DIRECTORIES)
                ),
                max_depth=int_option(
                    options, CONF_MAX_SCAN_DEPTH, DEFAULT_MAX_SCAN_DEPTH
                ),
                max_files_to_check=int_option(
                    options, CONF_MAX_FILES_TO_CHECK, DEFAULT_MAX_FILES_TO_CHECK
                ),
            )
            try:
                found_files, fetch_status = await async_fetch_files(
                    hass,
                    criteria,
                    wait_for_upload=call.data["wait_for_upload"],
                    timeout=call.data["timeout"],
                    quiet_period=call.data["quiet_period"],
                    fresh_extensions=set(call.data["fresh_extensions"]),
                    freshness_window=call.data["freshness_window"],
                    invocation_time_ns=invocation_time_ns,
                )
            except FileSearchError as err:
                _publish_error(
                    runtime, target_id, "invalid_search", "Invalid Search", str(err)
                )
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="invalid_search",
                    translation_placeholders={"error": str(err)},
                ) from err
            except OSError as err:
                _publish_error(
                    runtime, target_id, "os_error", "OS Error Walking Dir", str(err)
                )
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="filesystem_error",
                    translation_placeholders={
                        "directory": directory,
                        "error": str(err),
                    },
                ) from err
            if fetch_status["status"] != "ok":
                _LOGGER.warning(
                    "Degraded fetch result for target '%s': %s", target_id, fetch_status
                )
            result: TargetResult = cast(
                TargetResult,
                {
                    **build_file_results(found_files),
                    "timestamp": _timestamp(),
                    "FetchStatus": fetch_status,
                },
            )
            if not found_files:
                result["status"] = "No matching files"
            runtime.publish(target_id, result)
            if call.return_response:
                response: FetchResponse = {
                    "target_id": target_id,
                    "result": _response_value(result),
                }
                return cast(ServiceResponse, response)
            return None

    if not hass.services.has_service(DOMAIN, SERVICE_FETCH):
        hass.services.async_register(
            DOMAIN,
            SERVICE_FETCH,
            handle_fetch,
            schema=SERVICE_FETCH_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: FetchLatestFileConfigEntry
) -> bool:
    """Create an entry-owned runtime and its publication platform."""
    try:
        options = validate_options(entry.options)
        options = await hass.async_add_executor_job(validate_allowed_roots, options)
    except vol.Invalid as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="invalid_options",
            translation_placeholders={"error": str(err)},
        ) from err
    runtime = entry.runtime_data = FetchLatestFileRuntimeData(options=options)
    hass.data[DOMAIN] = runtime
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await runtime.async_stop()
        if hass.data.get(DOMAIN) is runtime:
            hass.data.pop(DOMAIN)
        raise
    runtime.expiry_task = entry.async_create_background_task(
        hass, runtime.async_expire(), "Fetch Latest File target expiry"
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: FetchLatestFileConfigEntry
) -> bool:
    """Stop operations, then unload publication without removing the action."""
    runtime = entry.runtime_data
    await runtime.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime.sensor_entity = None
        if hass.data.get(DOMAIN) is runtime:
            hass.data.pop(DOMAIN)
    else:
        runtime.unloading = False
        runtime.expiry_task = entry.async_create_background_task(
            hass, runtime.async_expire(), "Fetch Latest File target expiry"
        )
        _LOGGER.error("Failed to unload Fetch Latest File sensor platform")
    return unload_ok
