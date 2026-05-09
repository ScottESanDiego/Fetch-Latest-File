# Domain for the integration. Must match the folder name.
DOMAIN = "fetch_latest_file"

# Define the user-facing name
INTEGRATION_TITLE = "Fetch Latest File"

# Configuration option keys
CONF_ALLOWED_DIRECTORIES = "allowed_directories"
CONF_MAX_FILES_TO_CHECK = "max_files_to_check"
CONF_MAX_SCAN_DEPTH = "max_scan_depth"
CONF_MAX_TARGET_IDS = "max_target_ids"
CONF_TARGET_EXPIRY_HOURS = "target_expiry_hours"

# Default search limits
DEFAULT_ALLOWED_DIRECTORIES: tuple[str, ...] = ()
DEFAULT_MAX_FILES_TO_CHECK = 10000
DEFAULT_MAX_SCAN_DEPTH = 10

# Default cleanup limits for target_id results
DEFAULT_MAX_TARGET_IDS = 20  # Maximum number of target_ids to keep
DEFAULT_TARGET_EXPIRY_HOURS = 24  # Remove targets older than 24 hours
