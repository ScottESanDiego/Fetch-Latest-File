"""Config flow for Fetch Latest File integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    INTEGRATION_TITLE,
    CONF_MAX_TARGET_IDS,
    CONF_TARGET_EXPIRY_HOURS,
    DEFAULT_MAX_TARGET_IDS,
    DEFAULT_TARGET_EXPIRY_HOURS,
)


class ConfigFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Fetch Latest File."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)

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
            return self.async_create_entry(
                title=INTEGRATION_TITLE,
                data={},
                options={
                    CONF_MAX_TARGET_IDS: DEFAULT_MAX_TARGET_IDS,
                    CONF_TARGET_EXPIRY_HOURS: DEFAULT_TARGET_EXPIRY_HOURS,
                },
            )

        return self.async_show_form(step_id="user")


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Fetch Latest File."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_MAX_TARGET_IDS,
                        default=options.get(CONF_MAX_TARGET_IDS, DEFAULT_MAX_TARGET_IDS),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
                    vol.Optional(
                        CONF_TARGET_EXPIRY_HOURS,
                        default=options.get(CONF_TARGET_EXPIRY_HOURS, DEFAULT_TARGET_EXPIRY_HOURS),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),  # Max 1 week
                }
            ),
        )

