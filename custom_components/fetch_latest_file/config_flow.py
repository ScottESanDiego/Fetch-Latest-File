"""Config flow for Fetch Latest File integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ALLOWED_DIRECTORIES,
    CONF_MAX_FILES_TO_CHECK,
    CONF_MAX_SCAN_DEPTH,
    CONF_MAX_TARGET_IDS,
    CONF_TARGET_EXPIRY_HOURS,
    DEFAULT_ALLOWED_DIRECTORIES,
    DEFAULT_MAX_FILES_TO_CHECK,
    DEFAULT_MAX_SCAN_DEPTH,
    DEFAULT_MAX_TARGET_IDS,
    DEFAULT_TARGET_EXPIRY_HOURS,
    DOMAIN,
    INTEGRATION_TITLE,
)
from .runtime import validate_allowed_roots, validate_options
from .scanner import normalize_allowed_directories


def _allowed_directories_text(value: Any) -> str:
    """Format allowed directories for the options form."""
    return "\n".join(normalize_allowed_directories(value))


def _options_schema(options: Mapping[str, Any]) -> vol.Schema:
    """Return the options form schema."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_ALLOWED_DIRECTORIES,
                default=_allowed_directories_text(
                    options.get(CONF_ALLOWED_DIRECTORIES, DEFAULT_ALLOWED_DIRECTORIES)
                ),
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
            vol.Optional(
                CONF_MAX_SCAN_DEPTH,
                default=options.get(CONF_MAX_SCAN_DEPTH, DEFAULT_MAX_SCAN_DEPTH),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=100,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MAX_FILES_TO_CHECK,
                default=options.get(
                    CONF_MAX_FILES_TO_CHECK, DEFAULT_MAX_FILES_TO_CHECK
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=100000,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MAX_TARGET_IDS,
                default=options.get(CONF_MAX_TARGET_IDS, DEFAULT_MAX_TARGET_IDS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=100,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_TARGET_EXPIRY_HOURS,
                default=options.get(
                    CONF_TARGET_EXPIRY_HOURS, DEFAULT_TARGET_EXPIRY_HOURS
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=168,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
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
            return self.async_abort(
                reason="single_instance_allowed", translation_domain="homeassistant"
            )

        if user_input is not None:
            return self.async_create_entry(
                title=INTEGRATION_TITLE,
                data={},
                options={
                    CONF_ALLOWED_DIRECTORIES: list(DEFAULT_ALLOWED_DIRECTORIES),
                    CONF_MAX_SCAN_DEPTH: DEFAULT_MAX_SCAN_DEPTH,
                    CONF_MAX_FILES_TO_CHECK: DEFAULT_MAX_FILES_TO_CHECK,
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
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                options = validate_options(user_input)
                options = await self.hass.async_add_executor_job(
                    validate_allowed_roots, options
                )
            except vol.Invalid as err:
                errors["base"] = "invalid_options"
                description_placeholders["error"] = str(err)
            else:
                return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(user_input or self.config_entry.options),
            errors=errors,
            description_placeholders=description_placeholders,
        )
