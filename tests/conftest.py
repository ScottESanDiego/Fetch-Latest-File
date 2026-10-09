"""Real Home Assistant fixtures and host selector wakeup support."""

import pytest
import pytest_asyncio
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture(autouse=True)
def custom_components(enable_custom_integrations):
    """Allow the actual custom integration to load."""


@pytest_asyncio.fixture
async def entry(hass):
    """Set up a real config entry and sensor platform."""
    entry = MockConfigEntry(
        domain="fetch_latest_file", unique_id="fetch_latest_file", data={}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture(autouse=True, scope="session")
def bounded_host_selector():
    """Keep real selector/executor notifications responsive on restricted hosts."""
    import selectors
    from unittest.mock import patch

    select = selectors.EpollSelector.select

    def bounded_select(self, timeout=None):
        return select(self, 0.01 if timeout is None else min(timeout, 0.01))

    with patch.object(selectors.EpollSelector, "select", bounded_select):
        yield
