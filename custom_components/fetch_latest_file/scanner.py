"""File scanning helpers for Fetch Latest File."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

RESERVED_RESULT_KEYS = {"Overall", "timestamp", "status", "error", "error_details", "default"}
SIZE_MULTIPLIERS = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3}


class FileSearchError(ValueError):
    """Raised when a file search cannot be completed."""


@dataclass(frozen=True, slots=True)
class MatchedFile:
    """A file that matched the requested search criteria."""

    modified: float
    path: str
    extension: str


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    """Validated search criteria for a file scan."""

    directory: str
    filename_prefix: str
    extensions: set[str]
    min_size: int
    allowed_directories: list[str]
    max_depth: int
    max_files_to_check: int


def normalize_allowed_directories(value: Any) -> list[str]:
    """Normalize allowed directory option values to a clean list."""
    if not value:
        return []

    if isinstance(value, str):
        values = value.splitlines()
    else:
        try:
            values = [item for item in value if isinstance(item, str)]
        except TypeError:
            return []

    return [path.strip() for path in values if path.strip()]


def normalize_extensions(extensions: Any) -> set[str]:
    """Normalize service extension input to lowercase extension names."""
    if extensions is None:
        return set()

    if isinstance(extensions, str):
        extensions = [extensions]

    return {
        extension.lower().strip().lstrip(".")
        for extension in extensions
        if isinstance(extension, str) and extension.strip()
    }


def parse_min_size(min_size_str: str) -> int:
    """Parse a Home Assistant service min_size string into bytes."""
    min_size_str_upper = min_size_str.upper().strip()
    unit = next(
        (unit for unit in SIZE_MULTIPLIERS if min_size_str_upper.endswith(unit)),
        "B",
    )
    value_str = min_size_str_upper

    if unit != "B":
        value_str = value_str[:-len(unit)]
    else:
        value_str = value_str.rstrip("B")

    if not value_str:
        raise ValueError("Numeric size value missing.")

    min_size = int(value_str) * SIZE_MULTIPLIERS[unit]
    if min_size < 0:
        raise ValueError("Minimum size cannot be negative.")

    return min_size


def build_file_results(found_files: list[MatchedFile]) -> dict[str, str]:
    """Build sensor file result attributes from matched files."""
    sorted_files = sorted(found_files, key=lambda match: match.modified, reverse=True)
    file_results = {"Overall": sorted_files[0].path}

    for matched_file in sorted_files:
        extension_key = matched_file.extension or "no_extension"
        if extension_key in RESERVED_RESULT_KEYS:
            extension_key = f"ext_{extension_key}"

        file_results.setdefault(extension_key, matched_file.path)

    return file_results


def search_files(criteria: SearchCriteria) -> list[MatchedFile]:
    """Search for matching files. Runs in Home Assistant's executor."""
    base_path = _resolve_base_directory(criteria.directory)
    _validate_allowed_directory(base_path, criteria.allowed_directories)
    _validate_filename_prefix(criteria.filename_prefix)

    _LOGGER.debug(
        "Searching in '%s' for files starting with '%s'",
        base_path,
        criteria.filename_prefix,
    )
    if criteria.extensions:
        _LOGGER.debug("Filtering by extensions: %s", criteria.extensions)
    if criteria.min_size > 0:
        _LOGGER.debug("Filtering by minimum size: %d bytes", criteria.min_size)

    found_files: list[MatchedFile] = []
    file_count = 0

    for dirpath, dirnames, filenames in os.walk(base_path, followlinks=False):
        dirpath = Path(dirpath)
        depth = _directory_depth(base_path, dirpath)
        if depth is None:
            _LOGGER.warning("Skipping directory outside base path: %s", dirpath)
            dirnames.clear()
            continue

        if depth > criteria.max_depth:
            _LOGGER.debug("Skipping directory (too deep): %s", dirpath)
            dirnames.clear()
            continue

        if not _is_resolved_child(base_path, dirpath):
            _LOGGER.warning("Skipping directory outside base path: %s", dirpath)
            dirnames.clear()
            continue

        for filename in filenames:
            file_count += 1
            if file_count > criteria.max_files_to_check:
                _LOGGER.warning(
                    "Reached maximum file check limit (%d), stopping search",
                    criteria.max_files_to_check,
                )
                return found_files

            if not filename.lower().startswith(criteria.filename_prefix.lower()):
                continue

            matched_file = _match_file(
                base_path=base_path,
                file_path=dirpath / filename,
                extensions=criteria.extensions,
                min_size=criteria.min_size,
            )
            if matched_file is not None:
                found_files.append(matched_file)

    return found_files


def _resolve_base_directory(directory: str) -> Path:
    """Resolve and validate the requested search directory."""
    try:
        base_path = Path(directory).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as err:
        raise FileSearchError(f"Cannot resolve directory path: {directory}, Error: {err}") from err

    if not base_path.is_dir():
        raise FileSearchError(f"Invalid or inaccessible directory: {directory}")

    if not os.access(base_path, os.R_OK):
        raise FileSearchError(f"No read permission for directory: {base_path}")

    return base_path


def _validate_allowed_directory(base_path: Path, allowed_directories: list[str]) -> None:
    """Validate that the search directory is inside an allowed directory."""
    allowed_paths = _resolve_allowed_directories(allowed_directories)
    if not allowed_paths:
        return

    if any(
        base_path == allowed_path or base_path.is_relative_to(allowed_path)
        for allowed_path in allowed_paths
    ):
        return

    raise FileSearchError(f"Directory is not allowed: {base_path}")


def _resolve_allowed_directories(allowed_directories: list[str]) -> list[Path]:
    """Resolve configured allowed directories."""
    allowed_paths: list[Path] = []
    for directory in allowed_directories:
        try:
            allowed_path = Path(directory).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as err:
            raise FileSearchError(f"Cannot resolve allowed directory: {directory}, Error: {err}") from err

        if not allowed_path.is_dir():
            raise FileSearchError(f"Allowed path is not a directory: {allowed_path}")

        allowed_paths.append(allowed_path)

    return allowed_paths


def _validate_filename_prefix(filename_prefix: str) -> None:
    """Reject filename prefixes that look like paths."""
    if filename_prefix and (
        "/" in filename_prefix or "\\" in filename_prefix or ".." in filename_prefix
    ):
        raise FileSearchError(
            f"Invalid filename prefix contains path separators or '..' : {filename_prefix}"
        )


def _directory_depth(base_path: Path, dirpath: Path) -> int | None:
    """Return the current walk depth relative to the base path."""
    try:
        return len(dirpath.relative_to(base_path).parts)
    except ValueError:
        return None


def _is_resolved_child(base_path: Path, path: Path) -> bool:
    """Return whether a resolved path is within the resolved base path."""
    try:
        resolved_path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        _LOGGER.warning("Could not resolve path: %s", path)
        return False

    return resolved_path == base_path or resolved_path.is_relative_to(base_path)


def _match_file(
    base_path: Path,
    file_path: Path,
    extensions: set[str],
    min_size: int,
) -> MatchedFile | None:
    """Return a matched file if it passes security, extension, and size checks."""
    try:
        real_file_path = file_path.resolve(strict=True)
    except (OSError, RuntimeError):
        _LOGGER.warning("Could not resolve file path: %s", file_path)
        return None

    if not real_file_path.is_relative_to(base_path):
        _LOGGER.warning("Skipping file outside base directory: %s", file_path)
        return None

    try:
        stats = os.stat(file_path, follow_symlinks=False)
    except FileNotFoundError:
        _LOGGER.warning("File vanished during scan: %s", file_path)
        return None
    except OSError as err:
        _LOGGER.warning("OS error accessing stats for %s: %s", file_path, err)
        return None

    if file_path.is_symlink():
        _LOGGER.debug("Skipping symlink: %s", file_path)
        return None

    file_ext = file_path.suffix.lower().lstrip(".")
    if extensions and file_ext not in extensions:
        return None

    if stats.st_size < min_size:
        return None

    _LOGGER.debug(
        "Found matching file: %s (Extension: %s)",
        file_path,
        file_ext or "no_extension",
    )
    return MatchedFile(
        modified=stats.st_mtime,
        path=str(real_file_path),
        extension=file_ext,
    )
