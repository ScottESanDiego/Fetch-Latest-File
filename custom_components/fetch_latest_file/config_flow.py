"""Config flow for Fetch Latest File integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback

from .const import (
    DOMAIN,
    INTEGRATION_TITLE,
    CONF_MAX_TARGET_IDS,
    CONF_TARGET_EXPIRY_HOURS,
    DEFAULT_MAX_TARGET_IDS,
    DEFAULT_TARGET_EXPIRY_HOURS,
)


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(
            CONF_MAX_TARGET_IDS,
            default=DEFAULT_MAX_TARGET_IDS,
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        vol.Optional(
            CONF_TARGET_EXPIRY_HOURS,
            default=DEFAULT_TARGET_EXPIRY_HOURS,
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
    }
)


class ConfigFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Fetch Latest File."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()

    async def async_step_import(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle import from configuration.yaml."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"{INTEGRATION_TITLE} (imported)", data={})

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

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


class OptionsFlowHandler(OptionsFlowWithReload):
    """Handle options flow for Fetch Latest File."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA,
                self.config_entry.options,
            ),
        )
