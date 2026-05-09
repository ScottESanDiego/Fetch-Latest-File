# Home Assistant "Fetch Latest File" Custom Component

This custom component for Home Assistant allows you to retrieve ("fetch") the most recently modified files ("latest files"), such as camera screenshots and video events, of a certain minimum size from a specified directory. It was specifically designed for use with Reolink cameras and their integrations but can be easily adapted for a variety of other use cases.

Note that the original project is archived by the author. This form includes updates to make it work reliably with the latest Home Assistant versions.  As of version 3.0.0, this component is now a "Sensor" with improved async handling and modern Home Assistant best practices.

## Installation

1. Use HACS custom repository:
    [![Open your Home Assistant instance and show the add repository dialog of the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ScottESanDiego&repository=Fetch-Latest-File&category=integration) <details><summary>Manual Instructions</summary>
        1. Go to any of the sections (integrations, frontend, automation).
        2. Click on the 3 dots in the top right corner.
        3. Select "Custom repositories"
        4. Add this repository [URL](https://github.com/ScottESanDiego/Fetch-Latest-File) to the repository text field.
        5. Select the integration category.
        6. Click the "ADD" button. </details>
2. Go to Configuration > Integrations > Add Integration > **Fetch Latest File**

## Usage

Once you've set up the custom component in your Home Assistant instance, you can call it using the service `fetch_latest_file.fetch` with the following parameters:

- `directory`: The directory to search for files. *(Required)*
- `filename`: The start of the file name to search for. *(Required)*
- `extension`: The file extension(s) to search for. *(Optional)*
- `min_size`: The minimum size of the files to fetch. Specify the size as a string with a unit: B for bytes, K for kilobytes, M for megabytes, G for gigabytes. For example, "1M" for 1 megabyte. *(Optional)*
- `target_id`: A unique identifier for this fetch operation. Allows parallel execution and namespaced results. If not specified, uses "default". *(Optional)*

Here's an example of how to call this service:

```yaml
service: fetch_latest_file.fetch
data:
  directory: "/ftproot"
  filename: "Reolink-"
  extension: ["jpg", "mp4"]
  min_size: "1M"
```

This will search for the latest `.jpg` and `.mp4` files that start with "Reolink-" in the specified directory and are at least 1 megabyte in size. The result is then stored in entity state attributes which you can access in your automations, scripts, or templates.

### Sensor State and Attributes

The integration creates an entity **sensor.fetch_latest_file** that updates when the service is called.

- **State**: Timestamp of the most recent fetch operation (e.g., `2025-10-26T14:30:45-0700`)
- **Attributes**: Results organized by `target_id`, with each target containing:
  - `Overall`: The absolute latest file across all matched files
  - One attribute for each matched extension, such as `jpg`, `mp4`, or `txt`
  - `no_extension`: The latest matched file without a file extension, if any
  - `timestamp`: When this specific target was last updated

**Example attributes:**
```yaml
default:
  Overall: /ftproot/Reolink-OutdoorGarageNorth_20231015_143022.mp4
  mp4: /ftproot/Reolink-OutdoorGarageNorth_20231015_143022.mp4
  jpg: /ftproot/Reolink-OutdoorGarageNorth_20231015_143020.jpg
  timestamp: "2025-10-26T14:30:45-0700"
```

**Accessing attributes in templates:**
```yaml
# Access default target results (works without target_id)
{{ state_attr('sensor.fetch_latest_file', 'jpg') }}
{{ state_attr('sensor.fetch_latest_file', 'mp4') }}
{{ state_attr('sensor.fetch_latest_file', 'Overall') }}

# Access specific target results (when using target_id parameter)
{{ state_attr('sensor.fetch_latest_file', 'camera1')['Overall'] }}
{{ state_attr('sensor.fetch_latest_file', 'camera2')['jpg'] }}
{{ state_attr('sensor.fetch_latest_file', 'default')['mp4'] }}
```

**Note**: Only extensions with matching files will be present. Extension keys are lowercase and do not include the dot. If an extension conflicts with a reserved result key, such as `timestamp`, it is prefixed with `ext_`. If no files are found, the target will contain a `status: "No matching files"` entry.

## Parallel Execution

The `target_id` parameter enables safe parallel execution of the service. Service calls with different `target_id` values can run simultaneously, while calls with the same `target_id` execute sequentially to prevent conflicts.

**Example: Multiple cameras in parallel**
```yaml
# These can run simultaneously
- service: fetch_latest_file.fetch
  data:
    directory: "/ftproot/camera1"
    filename: "cam1-"
    extension: ["jpg", "mp4"]
    min_size: "1M"
    target_id: "camera1"

- service: fetch_latest_file.fetch
  data:
    directory: "/ftproot/camera2"
    filename: "cam2-"
    extension: ["jpg", "mp4"]
    min_size: "1M"
    target_id: "camera2"
```

## Configuration

The integration provides GUI-configurable options to control cleanup behavior:

1. Go to **Settings** → **Devices & Services**
2. Find **Fetch Latest File** integration
3. Click **Configure**
4. Adjust settings:
   - **Maximum number of target_ids to keep**: 1-100 (default: 20)
     - Limits how many different `target_id` results are stored
     - Oldest targets are removed when limit is exceeded
   - **Remove target_ids older than (hours)**: 1-168 hours (default: 24)
     - Automatically removes stale results
     - Prevents unbounded memory growth

These settings help manage the sensor's attribute storage and prevent it from growing indefinitely.

## Use Case

The main use case for this component is in a home security setup with Reolink cameras. Whenever an event is triggered, Home Assistant fetches the relevant files that meet the minimum size requirement and can post them to a specific Discord channel. This provides a streamlined way to access important security footage as soon as it is needed.

## Further Uses

This component can also be used in many other scenarios, such as:

- Fetching the latest screenshot from a home automation event that is of a certain size
- Retrieving the latest log files of a certain size for debugging purposes

Files are reported by their actual extension. For example, matching `.jpg`, `.png`, and `.mp4` files will produce `jpg`, `png`, and `mp4` attributes. Files without an extension are reported under `no_extension`.

## Support

Feel free to [open an issue](https://github.com/ScottESanDiego/Fetch-Latest-File/issues) for any problems or feature requests.
