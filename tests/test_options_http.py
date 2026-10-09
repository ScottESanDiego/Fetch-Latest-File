"""Exercise options schemas through Home Assistant's actual HTTP serializer."""

import pytest
from homeassistant.components.config import config_entries
from homeassistant.setup import async_setup_component

from custom_components.fetch_latest_file.runtime import OPTION_RULES

pytestmark = pytest.mark.asyncio

OPTIONS_URL = "/api/config/config_entries/options/flow"


@pytest.fixture
async def options_client(hass, hass_client):
    """Register the real options views on an authenticated HA HTTP server."""
    assert await async_setup_component(hass, "http", {})
    config_entries.async_setup(hass)
    return await hass_client()


async def start_options(client, entry):
    response = await client.post(OPTIONS_URL, json={"handler": entry.entry_id})
    assert response.status == 200
    result = await response.json()
    assert result["type"] == "form"
    return result


def fields(result):
    return {field["name"]: field for field in result["data_schema"]}


async def test_http_options_render_defaults_and_submit_reload(
    hass, entry, options_client, tmp_path
):
    """Rendering and saving use the production selector/JSON conversion path."""
    result = await start_options(options_client, entry)
    schema = fields(result)
    assert schema["allowed_directories"]["default"] == ""
    assert schema["allowed_directories"]["selector"] == {
        "text": {"multiline": True, "multiple": False}
    }
    for name, (default, minimum, maximum) in OPTION_RULES.items():
        assert schema[name]["default"] == default
        assert schema[name]["selector"] == {
            "number": {
                "min": minimum,
                "max": maximum,
                "step": 1,
                "mode": "box",
            }
        }

    runtime = entry.runtime_data
    submitted = {
        "allowed_directories": str(tmp_path),
        "max_scan_depth": 2,
        "max_files_to_check": 50,
        "max_target_ids": 3,
        "target_expiry_hours": 1,
    }
    response = await options_client.post(
        f"{OPTIONS_URL}/{result['flow_id']}", json=submitted
    )
    assert response.status == 200
    assert (await response.json())["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options == {
        **submitted,
        "allowed_directories": [str(tmp_path)],
    }
    assert all(isinstance(entry.options[name], int) for name in OPTION_RULES)
    assert entry.runtime_data is not runtime
    assert runtime.unloading

    reopened = fields(await start_options(options_client, entry))
    assert {name: field["default"] for name, field in reopened.items()} == submitted


@pytest.mark.parametrize("invalid", ["fraction", "bounds", "root"])
async def test_http_options_invalid_input_still_renders(
    hass, entry, options_client, tmp_path, invalid
):
    """Failures preserve the options and leave a serializable, usable form."""
    result = await start_options(options_client, entry)
    submitted = {
        "allowed_directories": str(tmp_path),
        "max_scan_depth": 2,
        "max_files_to_check": 50,
        "max_target_ids": 3,
        "target_expiry_hours": 1,
    }
    if invalid == "fraction":
        submitted["max_scan_depth"] = 1.5
    elif invalid == "bounds":
        submitted["max_target_ids"] = 101
    else:
        submitted["allowed_directories"] = str(tmp_path / "missing")
    url = f"{OPTIONS_URL}/{result['flow_id']}"
    runtime = entry.runtime_data
    response = await options_client.post(url, json=submitted)
    if invalid == "bounds":
        # HA rejects selector bounds before invoking the options step.
        assert response.status == 400
        assert "max_target_ids" in (await response.json())["errors"]
        response = await options_client.get(url)
        assert response.status == 200
        rendered = await response.json()
    else:
        assert response.status == 200
        rendered = await response.json()
        assert rendered["errors"] == {"base": "invalid_options"}
        assert {
            name: field["default"] for name, field in fields(rendered).items()
        } == submitted
    assert rendered["type"] == "form"
    assert len(fields(rendered)) == 5
    await hass.async_block_till_done()
    assert entry.options == {}
    assert entry.runtime_data is runtime
