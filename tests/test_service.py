"""Actual HA registration, action responses, sensor publication and lifecycle."""

import asyncio
import os
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import probatio as vol
import pytest
import yaml
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import fetch_latest_file as integration
from custom_components.fetch_latest_file.const import DOMAIN
from custom_components.fetch_latest_file.scanner import MatchedFile, parse_min_size

pytestmark = pytest.mark.asyncio


def data(tmp_path, **kwargs):
    return {"directory": str(tmp_path), "filename": "upload", **kwargs}


async def call(hass, tmp_path, response=True, **kwargs):
    return await hass.services.async_call(
        DOMAIN,
        "fetch",
        data(tmp_path, **kwargs),
        blocking=True,
        return_response=response,
    )


async def test_setup_response_and_sensor(hass, entry, tmp_path):
    path = tmp_path / "upload.jpg"
    path.write_bytes(b"complete")
    response = await call(hass, tmp_path)
    assert response["target_id"] == "default"
    assert response["result"]["Overall"] == str(path)
    assert isinstance(response["result"]["timestamp"], str)
    runtime = entry.runtime_data
    assert runtime.last_fetch == runtime.targets["default"]["timestamp"]
    assert not isinstance(runtime.targets["default"]["timestamp"], str)
    state = hass.states.get(runtime.sensor_entity.entity_id)
    assert state.attributes["default"]["Overall"] == str(path)
    assert await call(hass, tmp_path, response=False) is None
    assert runtime.locks == {}


async def test_optional_response_registration_without_yaml_response_metadata(
    hass, entry, tmp_path
):
    metadata = yaml.safe_load(
        (Path(integration.__file__).parent / "services.yaml").read_text()
    )
    assert "response" not in metadata["fetch"]
    assert hass.services.supports_response(DOMAIN, "fetch") is SupportsResponse.OPTIONAL
    (tmp_path / "upload.jpg").write_bytes(b"complete")
    assert (await call(hass, tmp_path))["result"]["Overall"].endswith("upload.jpg")
    assert await call(hass, tmp_path, response=False) is None


async def test_persistent_action_before_setup_and_after_unload(hass, tmp_path):
    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.services.has_service(DOMAIN, "fetch")
    with pytest.raises(HomeAssistantError) as caught:
        await call(hass, tmp_path)
    assert caught.value.translation_key == "unavailable"
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass.services.has_service(DOMAIN, "fetch")
    with pytest.raises(HomeAssistantError) as caught:
        await call(hass, tmp_path)
    assert caught.value.translation_key == "unavailable"
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert (await call(hass, tmp_path))["result"]["FetchStatus"]["status"] == "no_match"


@pytest.mark.parametrize(
    ("kwargs", "key"),
    [
        ({"min_size": "broken"}, "invalid_size"),
        ({"filename": "../upload"}, "invalid_search"),
    ],
)
async def test_translated_validation_clears_stale_paths(
    hass, entry, tmp_path, kwargs, key
):
    (tmp_path / "upload.jpg").write_bytes(b"complete")
    await call(hass, tmp_path)
    with pytest.raises(ServiceValidationError) as caught:
        await call(hass, tmp_path, **kwargs)
    assert caught.value.translation_key == key
    result = entry.runtime_data.targets["default"]
    assert "Overall" not in result
    assert result["FetchStatus"]["status"] == "error"
    await call(hass, tmp_path)
    assert entry.runtime_data.targets["default"]["FetchStatus"]["status"] == "ok"


async def test_translated_filesystem_error(hass, entry, tmp_path):
    with patch.object(integration, "async_fetch_files", side_effect=OSError("denied")):
        with pytest.raises(HomeAssistantError) as caught:
            await call(hass, tmp_path)
    assert caught.value.translation_key == "filesystem_error"
    assert caught.value.translation_placeholders["directory"] == str(tmp_path)
    assert entry.runtime_data.targets["default"]["FetchStatus"]["status"] == "error"


async def test_disabled_sensor_still_returns_then_enabled_publishes(hass, tmp_path):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_latest_file",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.sensor_entity is None
    (tmp_path / "upload.jpg").write_bytes(b"complete")
    assert (await call(hass, tmp_path))["result"]["Overall"].endswith("upload.jpg")
    registry.async_update_entity(entity.entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.sensor_entity is not None
    await call(hass, tmp_path)
    assert (
        hass.states.get(entity.entity_id)
        .attributes["default"]["jpg"]
        .endswith("upload.jpg")
    )


async def test_target_serialization_parallelism_and_bounded_locks(
    hass, entry, tmp_path
):
    entered = []
    release = asyncio.Event()

    async def fetch(hass, criteria, **kwargs):
        entered.append(criteria.filename_prefix)
        await release.wait()
        return [MatchedFile(1, "/upload.jpg", "jpg")], {"status": "ok", "reasons": {}}

    with patch.object(integration, "async_fetch_files", fetch):
        tasks = [
            asyncio.create_task(call(hass, tmp_path, filename=name, target_id=target))
            for name, target in [
                ("first", "same"),
                ("second", "same"),
                ("other", "other"),
            ]
        ]
        await asyncio.sleep(0.03)
        assert entered == ["first", "other"]
        assert entry.runtime_data.locks["same"].references == 2
        release.set()
        await asyncio.gather(*tasks)
    assert entered == ["first", "other", "second"]
    assert entry.runtime_data.locks == {}
    assert entry.runtime_data.active_fetches == set()


async def test_unload_cancels_active_queued_calls(hass, entry, tmp_path):
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def fetch(hass, criteria, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.02)
            cleaned.set()

    runtime = entry.runtime_data
    with patch.object(integration, "async_fetch_files", fetch):
        first = asyncio.create_task(call(hass, tmp_path))
        await entered.wait()
        queued = asyncio.create_task(call(hass, tmp_path))
        await asyncio.sleep(0.02)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert cleaned.is_set()
        for task in [first, queued]:
            with pytest.raises(asyncio.CancelledError):
                await task
    assert runtime.active_fetches == set()
    assert runtime.locks == {}
    assert runtime.sensor_entity is None
    assert runtime.expiry_task is None


async def test_expiry_removes_targets_keeps_timestamp(hass, entry, tmp_path):
    await call(hass, tmp_path)
    runtime = entry.runtime_data
    original = runtime.last_fetch
    with patch(
        "custom_components.fetch_latest_file.runtime.dt_util.utcnow",
        return_value=dt_util.utcnow() + timedelta(hours=25),
    ):
        assert runtime.cleanup()
        runtime.refresh_sensor()
    assert runtime.targets == {}
    assert runtime.last_fetch == original
    assert (
        hass.states.get(runtime.sensor_entity.entity_id).state
        == dt_util.as_utc(original).isoformat()
    )


async def test_schema_defaults_and_positive_durations():
    normalized = integration.SERVICE_FETCH_SCHEMA({"directory": "/tmp", "filename": ""})
    assert normalized["min_size"] == "0B"
    assert normalized["wait_for_upload"] is False
    assert normalized["timeout"] == 30
    assert normalized["quiet_period"] == 2
    assert normalized["fresh_extensions"] == []
    assert normalized["freshness_window"] == 5
    for field in ["MinSize", "minsize", "timeout", "quiet_period"]:
        for value in (
            ["1M"]
            if field.startswith("Min") or field == "minsize"
            else [0, -1, "nan", "inf"]
        ):
            with pytest.raises(vol.Invalid):
                integration.SERVICE_FETCH_SCHEMA(
                    {"directory": "/tmp", "filename": "", field: value}
                )


async def test_service_rejects_fresh_extension_outside_requested_filter(
    hass, entry, tmp_path
):
    with pytest.raises(vol.Invalid, match="must be included in extension"):
        await call(hass, tmp_path, extension=["jpg"], fresh_extensions=["png"])
    assert entry.runtime_data.targets == {}


@pytest.mark.parametrize("value", [0, -1, "nan", "inf", float("nan"), float("inf")])
async def test_service_rejects_nonpositive_or_nonfinite_freshness_window(
    hass, entry, tmp_path, value
):
    with pytest.raises(vol.Invalid, match="positive finite"):
        await call(hass, tmp_path, freshness_window=value)
    assert entry.runtime_data.targets == {}


async def test_service_passes_normalized_fresh_extensions_and_default_window(
    hass, entry, tmp_path
):
    received = []

    async def fetch(hass, criteria, **kwargs):
        received.append((criteria.extensions, kwargs))
        return [MatchedFile(1, "/upload.jpg", "jpg")], {"status": "ok", "reasons": {}}

    with patch.object(integration, "async_fetch_files", fetch):
        await call(
            hass,
            tmp_path,
            extension=[" .JPG ", "MP4"],
            fresh_extensions=[" ..JpG ", "jpg"],
            wait_for_upload=True,
        )

    [(extensions, passed)] = received
    assert extensions == {"jpg", "mp4"}
    assert passed["fresh_extensions"] == {"jpg"}
    assert passed["freshness_window"] == 5
    assert passed["wait_for_upload"] is True


async def test_user_and_options_flows_reload(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    entry = result["result"]
    runtime = entry.runtime_data
    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert duplicate["type"] == "abort"
    assert duplicate["reason"] == "single_instance_allowed"
    options = await hass.config_entries.options.async_init(entry.entry_id)
    assert options["type"] == "form"
    updated = await hass.config_entries.options.async_configure(
        options["flow_id"],
        {
            "allowed_directories": "/tmp",
            "max_scan_depth": 2,
            "max_files_to_check": 50,
            "max_target_ids": 3,
            "target_expiry_hours": 1,
        },
    )
    assert updated["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["allowed_directories"] == ["/tmp"]
    assert entry.options["max_scan_depth"] == 2
    assert entry.runtime_data is not runtime
    assert runtime.unloading


async def test_minute_expiry_without_new_fetch(hass, entry, tmp_path):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    await call(hass, tmp_path)
    runtime = entry.runtime_data
    original = runtime.last_fetch
    runtime.targets["default"]["timestamp"] = dt_util.now() - timedelta(hours=25)
    await asyncio.sleep(0)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert runtime.targets == {}
    assert runtime.last_fetch == original
    assert (
        hass.states.get(runtime.sensor_entity.entity_id).state
        == dt_util.as_utc(original).isoformat()
    )


@pytest.mark.parametrize(
    "options",
    [
        {"max_scan_depth": 1.5},
        {"max_scan_depth": True},
        {"max_scan_depth": -1},
        {"max_files_to_check": 0},
        {"max_target_ids": 101},
        {"target_expiry_hours": 169},
        {"allowed_directories": ["/does-not-exist-fetch-latest-tests"]},
    ],
)
async def test_invalid_stored_options_fail_setup(hass, options):
    from homeassistant.config_entries import ConfigEntryState

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert DOMAIN not in hass.data
    assert hass.services.has_service(DOMAIN, "fetch")


async def test_legacy_integer_options_normalize_on_setup(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={}, options={"max_scan_depth": "2"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.options["max_scan_depth"] == 2


async def test_invalid_options_form_preserves_values(hass, entry, tmp_path):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "allowed_directories": str(tmp_path / "missing"),
            "max_scan_depth": 2,
            "max_files_to_check": 50,
            "max_target_ids": 3,
            "target_expiry_hours": 1,
        },
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_options"}
    assert "missing" in result["description_placeholders"]["error"]
    assert entry.options == {}


async def test_response_diagnostics_are_isolated_from_runtime_and_sensor(
    hass, entry, tmp_path
):
    (tmp_path / "upload-old.jpg").write_bytes(b"large enough")
    old = tmp_path / "upload-old.jpg"
    os.utime(old, (10, 10))
    (tmp_path / "upload-new.jpg").write_bytes(b"x")
    response = await call(hass, tmp_path, min_size="5B")
    diagnostics = response["result"]["FetchStatus"]
    assert diagnostics["reasons"]["jpg"] == ["minimum_size"]
    diagnostics["reasons"]["jpg"].append("completion_unverified")
    diagnostics["reasons"]["Overall"] = ["upload_timeout"]
    diagnostics["status"] = "error"
    response["result"]["Overall"] = "mutated"
    expected = {
        "status": "fallback",
        "reasons": {"jpg": ["minimum_size"], "Overall": ["minimum_size"]},
    }
    assert entry.runtime_data.targets["default"]["FetchStatus"] == expected
    assert entry.runtime_data.targets["default"]["Overall"] == str(old)
    state = hass.states.get(entry.runtime_data.sensor_entity.entity_id)
    assert state.attributes["default"]["FetchStatus"] == expected
    assert state.attributes["default"]["Overall"] == str(old)


async def test_candidate_permission_failure_clears_paths_and_raises_translated_error(
    hass, entry, tmp_path
):
    path = tmp_path / "upload.jpg"
    path.write_bytes(b"complete")
    await call(hass, tmp_path)
    stat = os.stat

    def denied_stat(candidate, *args, **kwargs):
        if os.fspath(candidate) == str(path) and kwargs.get("follow_symlinks") is False:
            raise PermissionError("candidate stat denied")
        return stat(candidate, *args, **kwargs)

    with patch("custom_components.fetch_latest_file.scanner.os.stat", denied_stat):
        with pytest.raises(HomeAssistantError) as caught:
            await call(hass, tmp_path)
    assert caught.value.translation_key == "filesystem_error"
    assert "candidate stat denied" in caught.value.translation_placeholders["error"]
    result = entry.runtime_data.targets["default"]
    assert "Overall" not in result
    assert "jpg" not in result
    assert result["FetchStatus"] == {
        "status": "error",
        "reasons": {"Overall": ["os_error"]},
    }


@pytest.mark.parametrize("denied", ["file", "directory"])
async def test_permission_failure_during_resolution_is_filesystem_error(
    hass, entry, tmp_path, denied
):
    from pathlib import Path

    child = tmp_path / "child"
    child.mkdir()
    path = child / "upload.jpg"
    path.write_bytes(b"complete")
    await call(hass, tmp_path)
    resolve = Path.resolve
    target = path if denied == "file" else child

    def denied_resolve(candidate, *args, **kwargs):
        if candidate == target:
            raise PermissionError("path resolve denied")
        return resolve(candidate, *args, **kwargs)

    with patch.object(Path, "resolve", denied_resolve):
        with pytest.raises(HomeAssistantError) as caught:
            await call(hass, tmp_path)
    assert caught.value.translation_key == "filesystem_error"
    assert "Overall" not in entry.runtime_data.targets["default"]


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ("0B", 0),
        ("0", 0),
        ("123", 123),
        ("123B", 123),
        ("1K", 1024),
        ("1M", 1024**2),
        ("1G", 1024**3),
        ("1KB", 1024),
        ("1MB", 1024**2),
        ("1GB", 1024**3),
        (" 1mB ", 1024**2),
        ("+1M", 1024**2),
        ("1 M", 1024**2),
        ("1.5M", 1572864),
        (".5M", 524288),
        ("1.M", 1024**2),
        ("1.1", 2),
        ("0.1B", 1),
        ("0.0001K", 1),
        ("0.001M", 1049),
        ("1.00000000000000000000000000001B", 2),
    ],
)
async def test_min_size_decimal_formats_round_up_exactly(size, expected):
    assert parse_min_size(size) == expected


@pytest.mark.parametrize(
    "size",
    [
        "",
        "B",
        "-1",
        "-0B",
        "-.5M",
        "NaN",
        "Infinity",
        "infB",
        "1e3",
        "1e3M",
        "1KBX",
        "1MBB",
        "1BB",
        "1KK",
        "1KiB",
        "1.2.3M",
        ".M",
        "1 2M",
        "1K B",
        "++1M",
    ],
)
async def test_min_size_rejects_negative_nonfinite_and_malformed_values(size):
    with pytest.raises(ValueError):
        parse_min_size(size)


async def test_fractional_min_size_skips_file_below_rounded_byte_threshold(
    hass, entry, tmp_path
):
    old = tmp_path / "upload-old.jpg"
    old.write_bytes(b"ab")
    os.utime(old, (10, 10))
    newest = tmp_path / "upload-new.jpg"
    newest.write_bytes(b"a")
    response = await call(hass, tmp_path, min_size="1.00000000000000000000000000001B")
    assert response["result"]["Overall"] == str(old)
    assert response["result"]["FetchStatus"]["reasons"]["Overall"] == ["minimum_size"]


async def test_same_second_fetches_preserve_microseconds_in_state_and_response(
    hass, entry, tmp_path
):
    (tmp_path / "upload.jpg").write_bytes(b"complete")
    first_time = (
        dt_util.now()
        .astimezone(timezone(timedelta(hours=-7)))
        .replace(microsecond=123456)
    )
    second_time = first_time.replace(microsecond=234567)
    states = []
    responses = []
    for timestamp in (first_time, second_time):
        with patch.object(integration.dt_util, "now", return_value=timestamp):
            responses.append(await call(hass, tmp_path))
        states.append(hass.states.get(entry.runtime_data.sensor_entity.entity_id))
        assert entry.runtime_data.targets["default"]["timestamp"] == timestamp
        assert responses[-1]["result"]["timestamp"] == timestamp.isoformat()
        assert states[-1].state == dt_util.as_utc(timestamp).isoformat()
    assert states[0].state != states[1].state
    assert responses[0]["result"]["timestamp"] != responses[1]["result"]["timestamp"]


async def test_initial_sensor_timestamp_is_unknown(hass, entry):
    entity = entry.runtime_data.sensor_entity
    assert entity.state is None
    assert hass.states.get(entity.entity_id).state == "unknown"
    assert hass.states.get(entity.entity_id).attributes["device_class"] == "timestamp"


async def test_sensor_timestamp_keeps_home_assistant_naive_datetime_validation(
    hass, entry, tmp_path
):
    await call(hass, tmp_path)
    entity = entry.runtime_data.sensor_entity
    original = entity.native_value
    entity._attr_native_value = original.replace(tzinfo=None)
    try:
        with pytest.raises(ValueError, match="missing timezone information"):
            _ = entity.state
    finally:
        entity._attr_native_value = original
