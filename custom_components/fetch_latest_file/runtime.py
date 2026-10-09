"""Entry-owned results, publication, and operation lifecycle."""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import probatio as vol
from homeassistant.util import dt as dt_util

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
)
from .types import TargetResult

if TYPE_CHECKING:
    from .sensor import FetchLatestFileSensor


OPTION_RULES = {
    CONF_MAX_SCAN_DEPTH: (DEFAULT_MAX_SCAN_DEPTH, 0, 100),
    CONF_MAX_FILES_TO_CHECK: (DEFAULT_MAX_FILES_TO_CHECK, 1, 100000),
    CONF_MAX_TARGET_IDS: (DEFAULT_MAX_TARGET_IDS, 1, 100),
    CONF_TARGET_EXPIRY_HOURS: (DEFAULT_TARGET_EXPIRY_HOURS, 1, 168),
}


def integer_option(value: object, key: str) -> int:
    """Validate bounded integer options without truncating fractional values."""
    _, minimum, maximum = OPTION_RULES[key]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise vol.Invalid(f"{key} must be an integer")
    try:
        number = float(value)
    except (ValueError, OverflowError) as err:
        raise vol.Invalid(f"{key} must be an integer") from err
    if (
        not math.isfinite(number)
        or not number.is_integer()
        or not minimum <= number <= maximum
    ):
        raise vol.Invalid(f"{key} must be an integer between {minimum} and {maximum}")
    return int(number)


def int_option(options: Mapping[str, object], key: str, default: int) -> int:
    """Read a validated option, using defaults only for absent keys."""
    return integer_option(options.get(key, default), key)


def validate_options(options: Mapping[str, object]) -> dict[str, object]:
    """Shared setup/form validation, preserving option names and defaults."""
    from .scanner import normalize_allowed_directories

    result = dict(options)
    for key, (default, _, _) in OPTION_RULES.items():
        result[key] = integer_option(options.get(key, default), key)
    roots = options.get(CONF_ALLOWED_DIRECTORIES, DEFAULT_ALLOWED_DIRECTORIES)
    if not isinstance(roots, (str, list, tuple)) or (
        not isinstance(roots, str) and any(not isinstance(root, str) for root in roots)
    ):
        raise vol.Invalid("allowed_directories must contain directory names")
    result[CONF_ALLOWED_DIRECTORIES] = normalize_allowed_directories(roots)
    return result


def validate_allowed_roots(options: dict[str, object]) -> dict[str, object]:
    """Resolve and check configured roots in the executor before storing them."""
    from .scanner import normalize_allowed_directories

    roots = normalize_allowed_directories(options[CONF_ALLOWED_DIRECTORIES])
    resolved: list[str] = []
    for root in roots:
        try:
            path = Path(root).expanduser().resolve(strict=True)
            if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
                raise OSError("path must be a readable, searchable directory")
            with os.scandir(path):
                pass
        except (OSError, RuntimeError) as err:
            raise vol.Invalid(f"Invalid allowed directory {root}: {err}") from err
        resolved.append(str(path))
    return {**options, CONF_ALLOWED_DIRECTORIES: resolved}


@dataclass
class TargetLock:
    """A lock referenced by both queued and active operations."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    references: int = 0


@dataclass
class FetchLatestFileRuntimeData:
    """Own all mutable state independently of the sensor entity."""

    options: Mapping[str, object] = field(default_factory=dict)
    sensor_entity: FetchLatestFileSensor | None = None
    targets: dict[str, TargetResult] = field(default_factory=dict)
    last_fetch: datetime | None = None
    locks: dict[str, TargetLock] = field(default_factory=dict)
    active_fetches: set[asyncio.Task[Any]] = field(default_factory=set)
    expiry_task: asyncio.Task[None] | None = None
    unloading: bool = False

    @asynccontextmanager
    async def operation(self, target_id: str) -> AsyncIterator[None]:
        """Serialize one target while tracking queued calls for unload."""
        slot = self.locks.setdefault(target_id, TargetLock())
        slot.references += 1
        task = asyncio.current_task()
        if task is not None:
            self.active_fetches.add(task)
        try:
            async with slot.lock:
                yield
        finally:
            if task is not None:
                self.active_fetches.discard(task)
            slot.references -= 1
            if slot.references == 0:
                self.locks.pop(target_id, None)

    def publish(self, target_id: str, result: TargetResult) -> None:
        """Replace a complete target result, then refresh an added sensor."""
        self.targets[target_id] = result
        self.last_fetch = result["timestamp"]
        self.cleanup()
        self.refresh_sensor()

    def refresh_sensor(self) -> None:
        """Publish only through an entity that completed addition."""
        if self.sensor_entity is not None:
            self.sensor_entity.publish_runtime()

    def cleanup(self) -> bool:
        """Expire targets and cap storage without changing last-fetch state."""
        now = dt_util.utcnow().timestamp()
        expiry = (
            int_option(
                self.options, CONF_TARGET_EXPIRY_HOURS, DEFAULT_TARGET_EXPIRY_HOURS
            )
            * 3600
        )
        ordered = sorted(
            self.targets,
            key=lambda target: self.targets[target]["timestamp"].timestamp(),
        )
        remove = {
            target
            for target in ordered
            if now - self.targets[target]["timestamp"].timestamp() > expiry
        }
        remaining = [target for target in ordered if target not in remove]
        maximum = int_option(self.options, CONF_MAX_TARGET_IDS, DEFAULT_MAX_TARGET_IDS)
        remove.update(remaining[: max(0, len(remaining) - maximum)])
        for target in remove:
            self.targets.pop(target)
        return bool(remove)

    async def async_expire(self) -> None:
        """Reclaim stale results once a minute, even without further calls."""
        while True:
            await asyncio.sleep(60)
            if self.cleanup():
                self.refresh_sensor()

    async def async_stop(self) -> None:
        """Cancel and await every operation and the expiry loop."""
        self.unloading = True
        tasks: list[asyncio.Task[Any]] = list(self.active_fetches)
        if self.expiry_task is not None:
            tasks.append(self.expiry_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.expiry_task = None
