import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_UNIQUE_ID

# Import the updated domain
from .const import DOMAIN

# Use the updated domain in the class definition
class ConfigFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Fetch Latest File."""

    VERSION = 1 # Config flow version

    async def async_step_import(self, user_input=None):
        """Handle import from configuration.yaml."""
        # Use the updated domain
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        # Use the defined title
        return self.async_create_entry(title=f"{INTEGRATION_TITLE} (imported)", data={})


    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        # Use the updated domain
        await self.async_set_unique_id(DOMAIN)
        # This is the standard way to abort if an entry with this unique_id already exists
        self._abort_if_unique_id_configured()

        # Also keep the check for any current entries (belt and suspenders)
        if self._async_current_entries():
            return self.async_abort(reason='single_instance_allowed')


        # If user_input is not None, it means the user confirmed the step
        if user_input is not None:
             # Use the defined title
             return self.async_create_entry(title=INTEGRATION_TITLE, data={})

        # Show the confirmation form to the user.
        return self.async_show_form(step_id="user")

