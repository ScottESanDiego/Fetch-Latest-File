# Home Assistant "Fetch Latest file" Custom Component

This custom component for Home Assistant allows you to retrieve ("fetch") the most recently modified files ("latest files"), such as camera screenshots and video events, of a certain minimum size from a specified directory. It was specifically designed for use with Reolink cameras and their integrations but can be easily adapted for a variety of other use cases.

Note that the original project is (or soon will be!) archived by the author, and this form includes updates to make it work reliably with the latest Home Assistant versions.

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

OR

1. Copy the `fetch_latest_file` folder into your `custom_components` folder within your Home Assistant configuration directory.
2. Add the following to your `configuration.yaml`:

```yaml
fetch_latest_file:
```

## Usage

Once you've set up the custom component in your Home Assistant instance, you can call it using the service `latest.fetch` with the following parameters:

- `directory`: The directory to search for files. *(Required)*
- `filename`: The start of the file name to search for. *(Required)*
- `extension`: The file extension(s) to search for. *(Optional)*
- `min_size`: The minimum size of the files to fetch. Specify the size as a string with a unit: B for bytes, K for kilobytes, M for megabytes, G for gigabytes. For example, "1M" for 1 megabyte. *(Optional)*

Here's an example of how to call this service:

```yaml
service: fetch_latest_file.fetch
data:
  directory: "/path/to/your/directory"
  filename: "cam1"
  extension: ["jpg", "mp4"]
  min_size: "1M"
```

This will search for the latest `.jpg` and `.mp4` files that start with "cam1" in the specified directory and are at least 1 megabyte in size. The result is then stored in a entity state attribute which you can access in your automations, scripts, or templates.

Entity: **fetch_latest_file.file** 's attributes:
```
video: /path/to/your/directory/cam1_20230613102757.mp4
image: /path/to/your/directory/cam1_20230613102757.jpg
```

## Use Case

The main use case for this component is in a home security setup with Reolink cameras. Whenever an event is triggered, Home Assistant fetches the relevant files that meet the minimum size requirement and can post them to a specific Discord channel. This provides a streamlined way to access important security footage as soon as it is needed.

## Further Uses

This component can also be used in many other scenarios, such as:

- Fetching the latest screenshot from a home automation event that is of

 a certain size
- Retrieving the latest log files of a certain size for debugging purposes

This component is highly flexible and can be adapted to suit a variety of needs within your Home Assistant setup.
List of supported file extensions categories:

- Image: jpg, jpeg, png, gif, bmp, webp, svg, heic, raw
- Video: mp4, mkv, webm, flv, vob, ogv, avi, mov, wmv, mpg, mpeg, m4v
- Audio: mp3, flac, wav, aac, ogg, wma, m4a, opus
- Document: doc, docx, odt, pdf, rtf, tex, txt, wpd
- Spreadsheet: xls, xlsx, ods, csv
- Presentation: ppt, pptx, odp
- Web: html, htm, xhtml, xml, css, js, php, json
- Archive: zip, tar, gz, rar, 7z
- Executable: exe, msi, bin, command, sh, bat, crx
- Config: yaml, yml, ini, cfg, conf
- Log: log, txt, log, syslog, eventlog, debug, audit
- **Unknown**: none of the above.

## Support

Feel free to [open an issue](https://github.com/ScottESanDiego/Fetch-Lastest-File/issues) for any problems or feature requests.
