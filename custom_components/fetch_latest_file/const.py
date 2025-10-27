# Domain for the integration. Must match the folder name.
DOMAIN = "fetch_latest_file"

# Define the user-facing name
INTEGRATION_TITLE = "Fetch Latest File"

# Configuration option keys
CONF_MAX_TARGET_IDS = "max_target_ids"
CONF_TARGET_EXPIRY_HOURS = "target_expiry_hours"

# Default cleanup limits for target_id results
DEFAULT_MAX_TARGET_IDS = 20  # Maximum number of target_ids to keep
DEFAULT_TARGET_EXPIRY_HOURS = 24  # Remove targets older than 24 hours

# Define common extensions
IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'heic', 'raw'}
VIDEO_EXTS = {'mp4', 'mkv', 'webm', 'flv', 'vob', 'ogv', 'avi', 'mov', 'wmv', 'mpg', 'mpeg', 'm4v'}
AUDIO_EXTS = {'mp3', 'flac', 'wav', 'aac', 'ogg', 'wma', 'm4a', 'opus'}

