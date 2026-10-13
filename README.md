# Home Assistant "Fetch Latest File" Custom Component

Fetch the newest files from a directory for your Home Assistant automations. Filter by filename prefix, extension, and minimum size. Optionally wait for uploads to finish. Use the results for Reolink camera snapshots and videos, screenshots, logs, or other files.

The original author archived the project. This fork maintains compatibility with current Home Assistant versions and publishes results through a sensor and optional action responses.

## Installation

Version 4.2.2 requires Home Assistant 2026.10.0 or newer (Python 3.14.2 or newer).

1. Add this HACS custom repository and install the integration:

   [![Open your Home Assistant instance and show the add repository dialog of the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ScottESanDiego&repository=Fetch-Latest-File&category=integration)

   <details>
   <summary>Manual instructions</summary>

   In HACS, open the three-dot menu and select **Custom repositories**. Add the [repository URL](https://github.com/ScottESanDiego/Fetch-Latest-File), choose the **Integration** category, and click **Add**. Install **Fetch Latest File**.

   </details>

2. Go to **Settings → Devices & Services → Add Integration → Fetch Latest File**.

## Usage

After setup, call the `fetch_latest_file.fetch` action with these parameters:

- `directory`: The directory to search for files. *(Required)*
- `filename`: The start of the file name to search for. *(Required)*
- `extension`: The file extension(s) to search for. *(Optional)*
- `min_size`: Nonnegative minimum size, such as `1024`, `1KB`, `1MB`, or `1.5M`. Defaults to `0B`. See **Minimum size** below. *(Optional)*
- `wait_for_upload`: Wait for matching uploads to finish before publishing paths. Defaults to `false`. *(Optional)*
- `timeout`: Maximum upload wait in positive seconds. Starts when the call begins processing after earlier calls for the same target finish. Defaults to `30`. *(Optional)*
- `fresh_extensions`: A list of extensions requiring a recent upload while waiting. Entries ignore case, surrounding whitespace, and leading dots; normalized entries must be included in `extension`. Defaults to `[]`. Ignored when waiting is disabled. *(Optional)*
- `freshness_window`: Positive finite seconds before action invocation allowed for fresh-extension modification times. Defaults to `5`. *(Optional)*
- `quiet_period`: Positive seconds during which file identity, size, and modification time must remain unchanged without a writer-close notification. Defaults to `2`. *(Optional)*
- `target_id`: Identifies a group of results. Use 1-64 letters, numbers, underscores, or hyphens. Calls with different IDs can run in parallel. Defaults to `default`. *(Optional)*

All ten parameters appear in the visual action editor for automations and scripts and under **Developer tools → Actions**. Select **Fetch Latest File: Fetch latest file**. Enter the directory and **Filename prefix**, then enable the optional fields you need. **Wait for upload** stays visible; expand the initially collapsed **Upload settings** section for timeout, quiet period, fresh extensions, and freshness window.

Add each extension as a separate entry, such as `jpg` and `mp4`. Leave this field disabled or empty to search all extensions. Enter minimum size as text, such as `1M`. Use the toggle for upload waiting and enter timeout and quiet period in seconds.

The four fields in **Upload settings** are ignored when **Wait for upload** is off. **Freshness window** only applies when **Fresh extensions** is nonempty. Sections only group controls visually: YAML and API action data still use the same flat parameter names, including `filename`.

After installing an update to the service field definitions, restart Home Assistant and refresh the browser before reopening the action editor.

For example:

```yaml
action: fetch_latest_file.fetch
data:
  directory: "/ftproot"
  filename: "Reolink-"
  extension: ["jpg", "mp4"]
  min_size: "1M"
  wait_for_upload: true
  timeout: 30
  quiet_period: 2
```

This searches `/ftproot` for the newest `.jpg` and `.mp4` files starting with `Reolink-`, each at least 1 MiB (1,048,576 bytes). Use the sensor attributes in automations, scripts, or templates.

### Minimum size

Size parsing uses exact decimal values. Existing integer formats such as `1024`, `1B`, `1K`, `1M`, and `1G` still work. Units ignore case: bare values and `B` mean bytes; `K`/`KB`, `M`/`MB`, and `G`/`GB` use powers of 1024. Decimals such as `1.5M` and units such as `KB`, `MB`, and `GB` also work.

Fractional bytes round up: `1.1B` requires 2 bytes. You may include whitespace around the value or before the unit, and a leading plus sign. Values must be nonnegative.

### Sensor State and Attributes

The integration creates **sensor.fetch_latest_file** and updates it on each call.

- **State**: Timestamp of the most recent fetch operation, retaining microseconds in UTC (e.g., `2025-10-26T21:30:45.123456+00:00`)
- **Attributes**: Results organized by `target_id`, with each target containing:
  - `Overall`: The newest selected file across matched extensions
  - One attribute for each matched extension, such as `jpg`, `mp4`, or `txt`
  - `no_extension`: The latest matched file without a file extension, if any
  - `timestamp`: When this target last updated, as a datetime in Home Assistant’s local timezone
  - `FetchStatus`: Diagnostics for this call, containing `status` and `reasons`

**Example attributes:**

```yaml
default:
  Overall: /ftproot/Reolink-OutdoorGarageNorth_20231015_143022.mp4
  mp4: /ftproot/Reolink-OutdoorGarageNorth_20231015_143022.mp4
  jpg: /ftproot/Reolink-OutdoorGarageNorth_20231015_143020.jpg
  timestamp: "2025-10-26T14:30:45-0700"
  FetchStatus:
    status: ok
    reasons: {}
```

**Accessing attributes in templates:**

```yaml
# Access default target results (works without target_id)
{{ state_attr('sensor.fetch_latest_file', 'default')['jpg'] }}
{{ state_attr('sensor.fetch_latest_file', 'default')['mp4'] }}
{{ state_attr('sensor.fetch_latest_file', 'default')['Overall'] }}

# Access specific target results (when using target_id parameter)
{{ state_attr('sensor.fetch_latest_file', 'camera1')['Overall'] }}
{{ state_attr('sensor.fetch_latest_file', 'camera2')['jpg'] }}
{{ state_attr('sensor.fetch_latest_file', 'default')['mp4'] }}
```

Only extensions with matching files appear. Extension keys are lowercase without the dot. Results always appear under a `target_id`, including `default`. If no files match, the target contains `status: "No matching files"`.

To prevent key collisions, actual extensions matching reserved result keys, literal `no_extension`, or any `ext_` prefix receive one extra `ext_` prefix. For example, `.overall` becomes `ext_overall`, `.ext_overall` becomes `ext_ext_overall`, and `.no_extension` becomes `ext_no_extension`. Files without an extension retain `no_extension`.

### Upload completion and fallback

With `wait_for_upload: false`, the action selects the newest file meeting the filename, extension, and minimum-size criteria. It skips undersized files and selects the next newest qualifying file. Omit `extension`, or supply an empty filter, to report all matched extensions.

With `wait_for_upload: true`, the action watches for filesystem notifications before scanning. On local Linux storage, it uses inotify writer-close (`CLOSE_WRITE`) notifications. A close notification can make a file ready as soon as the action rechecks it.

Waiting pins the newest matching file per extension, including files still below the minimum size. A pin identifies its path, device, and inode; later arrivals never replace it. Even older completed files require an observed quiet period unless a writer-close notification is received. The action rechecks identity and content metadata before publishing and rescans to recover missed notifications. Notification failures add `notifications_unavailable` and use stability checks within the original timeout.

Use `fresh_extensions: [jpg]` alongside `extension: [jpg, mp4]` to require a recent image while allowing an existing video without a freshness cutoff. The action captures invocation time before queuing behind another call for the same target. The fixed cutoff is invocation time minus `freshness_window` (default five seconds); it never advances during the wait. On the initial scan, the newest eligible file is pinned, including an upload still in progress. If none exists, the first subsequently observed eligible candidate is pinned. When candidates first appear together, the earliest modification time wins, then the lexicographically smallest path. Previously excluded files may become eligible when uploaded or updated. Modification times describe filesystem writes, not camera capture relevance. An unfinished file last modified before the cutoff is excluded; increase the window if needed.

An event image may already be uploaded, still be uploading, or not have started uploading when the call arrives. A recent existing image is pinned and its completion is checked. An eligible partial image is pinned while it finishes. If no eligible image exists yet, the action waits for the next one instead of returning an older completed image. Old modification times alone never prove completion; readiness requires a writer-close notification or an observed quiet period.

Every fresh extension must have a completed, size-qualified pin before a successful return. Ordinary missing extensions do not delay otherwise ready results. Pinned uploads below `min_size` remain pending while growing. At timeout, only the same pinned size-qualified files are returned, with incomplete completion marked unverified. Deleted or replaced pins and undersized pins produce no path; older completed files are never substituted. `Overall` is the newest of the selected paths. No-wait selection remains unchanged even when freshness parameters are supplied.

Each call replaces `FetchStatus`. Its `status` is `ok`, `fallback`, `no_match`, or `error`. Its `reasons` maps affected result keys to lists of these codes:

| Code | Meaning |
| --- | --- |
| `minimum_size` | A newer matching file was skipped because it was too small. |
| `upload_timeout` | The upload deadline expired before a pinned file was ready or a required fresh file was found. |
| `completion_unverified` | The returned file may still be uploading. |
| `no_matching_file` | No qualifying result exists for a requested extension. |
| `scan_limit_reached` | A scan reached the configured file or directory limit; results may be incomplete. |
| `notifications_unavailable` | Notifications failed or were unavailable; heuristic checks were used. |
| `invalid_size` | The `min_size` value could not be parsed. |
| `invalid_search` | Directory or filename search validation failed. |
| `os_error` | An operating-system error prevented the search. |

Diagnostics also cover affected `Overall` selections. The action logs degraded outcomes as warnings. For example, the pinned MP4 is still uploading at timeout:

```yaml
default:
  Overall: /ftproot/Reolink-pinned.mp4
  mp4: /ftproot/Reolink-pinned.mp4
  timestamp: "2025-10-26T14:30:45-0700"
  FetchStatus:
    status: fallback
    reasons:
      Overall: [upload_timeout, completion_unverified]
      mp4: [upload_timeout, completion_unverified]
```

Timeout fallback completes the call without an upload-timeout error so automations can continue. If no file qualifies, the result contains `status: "No matching files"` and `FetchStatus.status: no_match`, with no file path.

Invalid minimum sizes and searches raise translated validation errors. Filesystem failures raise translated action errors. Before raising, the action removes stale paths for the target and sets `FetchStatus.status: error`. An unavailable integration also raises an action error.

Upload detection works best on local Linux storage when the uploader closes its final file. Writer closure does not prove that the transfer succeeded. The quiet-period check is heuristic: a paused upload can appear complete. The action cannot recover a close event from before it began watching.

An unverified timeout result lets automations continue but may point to an unfinished file. Check `FetchStatus` when completion matters.

### Optional action responses

Automations and scripts can capture the result directly with `response_variable`:

```yaml
- action: fetch_latest_file.fetch
  data:
    directory: "/ftproot"
    filename: "Reolink-"
    extension: ["jpg", "mp4"]
    wait_for_upload: true
    target_id: "camera1"
  response_variable: latest_files
- action: persistent_notification.create
  data:
    message: "{{ latest_files['result'].get('Overall', 'No matching file') }}"
```

The response is `{target_id, result}`. It contains the same target result as the sensor, with `timestamp` as an ISO string. API callers request this optional response with `return_response`. Existing calls need no change.

Target timestamps use Home Assistant’s local timezone. The sensor state uses UTC and retains microseconds, so fetches within the same second can produce distinct states. Disabling the sensor does not stop fetching or action responses while the integration is loaded.

## Parallel Execution

Calls with different `target_id` values can run at the same time. Calls with the same ID run one at a time.

**Example: Multiple cameras in parallel**

```yaml
# These can run simultaneously
- action: fetch_latest_file.fetch
  data:
    directory: "/ftproot/camera1"
    filename: "cam1-"
    extension: ["jpg", "mp4"]
    min_size: "1M"
    target_id: "camera1"

- action: fetch_latest_file.fetch
  data:
    directory: "/ftproot/camera2"
    filename: "cam2-"
    extension: ["jpg", "mp4"]
    min_size: "1M"
    target_id: "camera2"
```

## Configuration

Set search and result-cleanup options in the integration settings:

1. Go to **Settings** → **Devices & Services**
2. Find the **Fetch Latest File** integration
3. Click **Configure**
4. Adjust settings:
   - **Allowed directories**: One directory per line (default: blank)
     - Calls can scan only directories inside these roots when you set them
     - Configure explicit roots limited to the directories your automations need
     - Leave blank to allow any readable directory, matching earlier behavior
   - **Maximum subdirectory depth to scan**: 0-100 (default: 10)
     - `0` scans only the requested directory
   - **Maximum files to check per service call**: 1-100000 (default: 10000)
     - Bounds each scan’s work
   - **Maximum number of target_ids to keep**: 1-100 (default: 20)
     - Caps stored `target_id` results and removes the oldest when the limit is exceeded
   - **Remove target_ids older than (hours)**: 1-168 hours (default: 24)
     - Removes stale results once a minute, even without further calls

Cleanup preserves the sensor’s last-fetch timestamp. Directory traversal errors raise action errors. Reaching a scan limit returns available results with `scan_limit_reached` diagnostics.

## Uses

For Reolink security cameras, an automation can fetch event snapshots and videos that meet a minimum size, then post them to a Discord channel. You can also fetch screenshots after home automation events or retrieve the latest logs for debugging.

## Upgrading from 3.x.x to 4.y.y

Version 4 changes the sensor attributes in ways that can break automations, templates, scripts, and dashboard cards written for version 3.

Before upgrading:

1. Make sure your Home Assistant installation is version 2026.10.0 or newer.
2. Find any references to `sensor.fetch_latest_file` attributes in automations, scripts, templates, and dashboards.
3. Update those references to use the new `target_id` structure and extension-based attribute names.

Version 4 replaces broad media types with matched extensions.

Version 3 examples:

```yaml
{{ state_attr('sensor.fetch_latest_file', 'Image') }}
{{ state_attr('sensor.fetch_latest_file', 'Video') }}
{{ state_attr('sensor.fetch_latest_file', 'Overall') }}
```

Version 4 equivalents:

```yaml
{{ state_attr('sensor.fetch_latest_file', 'default')['jpg'] }}
{{ state_attr('sensor.fetch_latest_file', 'default')['mp4'] }}
{{ state_attr('sensor.fetch_latest_file', 'default')['Overall'] }}
```

If you use `target_id`, replace `default` with that target ID:

```yaml
{{ state_attr('sensor.fetch_latest_file', 'front_door')['jpg'] }}
```

Other upgrade notes:

- Attribute names are lowercase extensions without the dot, such as `jpg`, `png`, `mp4`, or `txt`.
- Files without an extension are reported as `no_extension`.
- Actual reserved extensions, literal `no_extension`, and extensions beginning with `ext_` receive one extra `ext_` prefix. Ordinary extension keys remain unchanged.
- `target_id` values must be 1-64 characters and can only contain letters, numbers, underscores, and hyphens.
- Results appear under a `target_id`, including `default`, rather than at the top level of the sensor attributes.
- New scan limits default to 10 subdirectory levels and 10000 files per service call. If a scan that worked in 3.x.x stops finding files, check the integration options and raise those limits if needed.
- The new **Allowed directories** option defaults to blank, which preserves the older behavior of allowing any readable directory. If you configure allowed directories, service calls can only scan inside those roots.

## Upgrading to 4.2.0

Update Home Assistant to 2026.10.0 or newer before installing 4.2.0. Upload waiting remains optional, and existing calls keep their defaults. You can adopt action responses without changing sensor templates. Review automations that depend on invalid-input calls continuing: these calls now raise translated errors. Timeouts and no-match outcomes still complete without raising.

If you use unusual extensions, add the extra `ext_` prefix to keys for literal `no_extension` and every `ext_` prefix, as described above. Ordinary extensions such as `jpg` and `mp4` keep their keys.

### Version 4.2.2 fixes

Upload waiting now pins file identity, waits for observed completion, and never substitutes later arrivals or older completed files. Optional per-extension freshness uses a fixed configurable recency window to await an event image while allowing an existing video. No-wait behavior is unchanged.

### Version 4.2.1 fixes

Unexpected notification failures now fall back to stability checks within the original timeout and report `notifications_unavailable`. Notification failures and repeated cancellation release filesystem watches. Action responses remain optional and work with the visual action editor. The integration options form now opens and saves correctly.

## Support

[Open an issue](https://github.com/ScottESanDiego/Fetch-Latest-File/issues) to report a problem or request a feature.
