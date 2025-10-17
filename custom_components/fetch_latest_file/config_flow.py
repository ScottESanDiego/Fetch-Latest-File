"""Config flow for Fetch Latest File integration."""
from __future__ import annotations

from homeassistant import config_entries

from .const import DOMAIN, INTEGRATION_TITLE


class ConfigFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Fetch Latest File."""

    VERSION = 1

    async def async_step_import(self, user_input=None):
        """Handle import from configuration.yaml."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"{INTEGRATION_TITLE} (imported)", data={})

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if self._async_current_entries():
            return self.async_abort(reason='single_instance_allowed')

        if user_input is not None:
            return self.async_create_entry(title=INTEGRATION_TITLE, data={})

        return self.async_show_form(step_id="user")

