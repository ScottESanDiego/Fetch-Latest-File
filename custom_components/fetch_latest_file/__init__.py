import os
import logging
import time
from typing import Any # Import Any for type hinting hass.data

from homeassistant.core import HomeAssistant, ServiceCall, callback # Import callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN # Assuming const.py exists with DOMAIN = "flf"
# Make sensor entity accessible for type hinting if possible, otherwise use 'Any'
# from .sensor import FetchLatestFileSensor # This might cause circular import issues, use carefully or avoid

_LOGGER = logging.getLogger(__name__)

# Define common image and video extensions (could be in const.py)
IMG_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'heic', 'raw'}
VID_EXTS = {'mp4', 'mkv', 'webm', 'flv', 'vob', 'ogv', 'avi', 'mov', 'wmv', 'mpg', 'mpeg', 'm4v'}

# Define platforms to set up
PLATFORMS = ["sensor"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FetchLatestFile from a config entry."""
    _LOGGER.info(f"Setting up FetchLatestFile integration entry_id: {entry.entry_id}")

    # Ensure hass.data structure exists for this domain and entry
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})

    # Set up the sensor platform using the correct function
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # --- Service Handler Definition ---
    # Define the service handler within async_setup_entry to capture hass and entry
    @callback # Mark as callback since it's called by the event loop (service call)
    def handle_fetch(call: ServiceCall) -> None:
        """Handle the service call to fetch the latest file(s) and update the sensor."""
        _LOGGER.debug(f"Service flf.fetch called with data: {call.data}")

        # Retrieve the sensor entity instance stored during platform setup
        # Use 'Any' for type hint if direct import causes issues
        sensor_entity: Any = hass.data[DOMAIN][entry.entry_id].get('sensor_entity')
        if not sensor_entity:
            _LOGGER.error("FetchLatestFile sensor entity not found in hass.data. Cannot update state.")
            # Attempt to update sensor state even if not found initially might be complex,
            # logging error is the primary action here.
            return

        # --- Service Call Parameter Processing ---
        normalized_data = {k.lower(): v for k, v in call.data.items()}
        directory = normalized_data.get('directory')
        file_name_prefix = normalized_data.get('filename')
        extensions = normalized_data.get('extension')
        min_size_str = normalized_data.get('min_size', "0B")

        # --- Parameter Validation ---
        # Validate directory
        if not isinstance(directory, str) or not os.path.isdir(directory):
            _LOGGER.error(f"Invalid or inaccessible directory: {directory}")
            # Update sensor state to reflect the error
            sensor_entity.update_data("Error: Invalid Directory", {"error_details": f"Path: {directory}"})
            return
        # Validate filename prefix
        if not isinstance(file_name_prefix, str):
            _LOGGER.error(f"Invalid 'filename' prefix: {file_name_prefix}")
            sensor_entity.update_data("Error: Invalid Filename", {"error_details": f"Filename prefix: {file_name_prefix}"})
            return
        # Validate and normalize extensions
        if extensions is None: extensions = []
        elif isinstance(extensions, str): extensions = [extensions]
        elif not isinstance(extensions, list):
             _LOGGER.error(f"Invalid 'extension' input: {extensions}")
             sensor_entity.update_data("Error: Invalid Extension", {"error_details": f"Extensions: {extensions}"})
             return
        cleaned_extensions = {ext.lower().strip('.') for ext in extensions if isinstance(ext, str)}

        # --- Minimum Size Parsing ---
        min_size = 0
        size_multiplier = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3}
        min_size_str_upper = min_size_str.upper().strip()
        try:
            # Determine unit and extract numeric value
            unit = next((u for u in size_multiplier if min_size_str_upper.endswith(u)), "B")
            value_str = min_size_str_upper
            if unit != "B": value_str = value_str[:-len(unit)]
            else: value_str = value_str.rstrip('B')
            if not value_str: raise ValueError("Numeric size value missing.")
            min_size = int(value_str) * size_multiplier[unit]
            if min_size < 0: raise ValueError("Minimum size cannot be negative.")
        except ValueError as e:
            _LOGGER.error(f"Invalid 'min_size' input '{min_size_str}': {e}")
            sensor_entity.update_data("Error: Invalid Size", {"error_details": f"Input: {min_size_str}, Error: {e}"})
            return
        except Exception as e: # Catch other potential parsing errors
            _LOGGER.error(f"Error parsing 'min_size' '{min_size_str}': {e}")
            sensor_entity.update_data("Error: Size Parsing Failed", {"error_details": f"Input: {min_size_str}, Error: {e}"})
            return

        # --- File Searching ---
        _LOGGER.debug(f"Searching in '{directory}' for files starting with '{file_name_prefix}'")
        if cleaned_extensions: _LOGGER.debug(f"Filtering by extensions: {cleaned_extensions}")
        if min_size > 0: _LOGGER.debug(f"Filtering by minimum size: {min_size_str} ({min_size} bytes)")

        found_files = [] # List to store (mod_time, file_path, file_ext, file_type)
        try:
            # Walk through directory recursively
            for dirpath, _, filenames in os.walk(directory):
                for filename in filenames:
                    # Check prefix match (case-insensitive)
                    if filename.lower().startswith(file_name_prefix.lower()):
                        file_path = os.path.join(dirpath, filename)
                        try:
                            # Get file stats
                            stats = os.stat(file_path)
                            mod_time = stats.st_mtime
                            file_size = stats.st_size
                            file_ext = os.path.splitext(filename)[1].lower().strip('.')

                            # Apply filters
                            extension_match = not cleaned_extensions or file_ext in cleaned_extensions
                            size_match = file_size >= min_size

                            if extension_match and size_match:
                                # Determine file type
                                file_type = "generic"
                                if file_ext in IMG_EXTS: file_type = "image"
                                elif file_ext in VID_EXTS: file_type = "video"
                                # Add file details to list
                                found_files.append((mod_time, file_path, file_ext, file_type))
                                _LOGGER.debug(f"Found matching file: {file_path} (Type: {file_type})")

                        except FileNotFoundError:
                            _LOGGER.warning(f"File vanished during scan: {file_path}")
                        except OSError as e:
                            _LOGGER.warning(f"OS error accessing stats for {file_path}: {e}")
        except OSError as e:
            # Handle errors during directory walk (e.g., permission denied)
            _LOGGER.error(f"OS error walking directory {directory}: {e}")
            sensor_entity.update_data(f"Error: OS Error Walking Dir", {"error_details": f"Directory: {directory}, Error: {e}"})
            return

        # --- Process Found Files ---
        if not found_files:
            _LOGGER.info("No matching files found for the criteria.")
            # Update sensor state to indicate no files found
            sensor_entity.update_data("No matching files", {})
            return

        # Sort files by modification time, newest first
        found_files.sort(key=lambda x: x[0], reverse=True)

        # Get the path of the absolute latest file
        overall_latest_file_path = found_files[0][1]

        # Find the latest file for each type (Image, Video, Generic)
        latest_by_type = {}
        processed_types = set()
        for mod_time, file_path, file_ext, file_type in found_files:
            if file_type not in processed_types:
                latest_by_type[file_type] = file_path
                processed_types.add(file_type)
            # Optimization: stop if all needed types are found
            if len(processed_types) >= 3: # Assuming only image, video, generic
                break

        # --- Prepare State and Attributes for Sensor ---
        state_attributes = {}
        # Use the attribute keys defined previously ('Overall', 'Image', 'Video', 'Generic')
        state_attributes['Overall'] = overall_latest_file_path
        if 'image' in latest_by_type:
            state_attributes['Image'] = latest_by_type['image']
        if 'video' in latest_by_type:
            state_attributes['Video'] = latest_by_type['video']
        if 'generic' in latest_by_type:
            state_attributes['Generic'] = latest_by_type['generic']

        # Use timestamp as the main state for the sensor for consistency
        current_time_state = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        # --- Update Sensor Entity ---
        # Call the update method on the sensor instance, which handles scheduling the state update
        sensor_entity.update_data(state=current_time_state, attributes=state_attributes)

        _LOGGER.info(f"Fetch completed. Sensor '{sensor_entity.entity_id}' updated.")
        _LOGGER.debug(f"Sensor state: {current_time_state}, attributes: {state_attributes}")

    # Register the 'fetch' service, pointing to the handler function defined above
    hass.services.async_register(DOMAIN, "fetch", handle_fetch)

    # Return True to indicate successful setup of the config entry
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading FetchLatestFile integration...")

    # Unregister the service first
    hass.services.async_remove(DOMAIN, "fetch")

    # Unload platforms individually by iterating through PLATFORMS
    # *** MODIFIED BLOCK BELOW ***
    unload_results = []
    for platform in PLATFORMS:
        try:
            result = await hass.config_entries.async_forward_entry_unload(entry, platform)
            unload_results.append(result)
            _LOGGER.debug(f"Unload result for platform {platform}: {result}")
        except Exception as e:
            _LOGGER.error(f"Error unloading platform {platform}: {e}")
            unload_results.append(False) # Assume failure on exception

    # Check if all platforms unloaded successfully
    unload_ok = all(unload_results)
    # *** END MODIFIED BLOCK ***

    # Clean up hass.data associated with this config entry
    if unload_ok:
        if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
            # Remove the specific entry's data
            hass.data[DOMAIN].pop(entry.entry_id)
            # If the domain data is now empty, remove the domain key itself
            if not hass.data[DOMAIN]:
                 hass.data.pop(DOMAIN)
        _LOGGER.info("FetchLatestFile integration successfully unloaded.")
    else:
        _LOGGER.error("Failed to unload one or more FetchLatestFile platforms.")

    return unload_ok

# Note: Legacy setup function (def setup(...)) should not be present
# if using config flow and async_setup_entry.

