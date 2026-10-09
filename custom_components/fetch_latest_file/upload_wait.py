"""Notification-driven upload readiness with bounded, graceful fallback."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

from .scanner import (
    MatchedFile,
    ScanResult,
    SearchCriteria,
    newest_files,
    refresh_files,
    result_key,
    scan_files,
    watch_directories,
)
from .types import FetchStatus, ReasonCode

_LOGGER = logging.getLogger(__name__)
_newest = newest_files


def _status(
    scan: ScanResult,
    selected: dict[str, MatchedFile],
    requested: set[str],
    timed_out: bool,
    ready: set[str],
    notifications_failed: bool = False,
) -> FetchStatus:
    reasons: dict[str, list[ReasonCode]] = {}
    newest = _newest(scan.files)
    small = _newest(scan.undersized)
    for key, match in selected.items():
        codes: list[ReasonCode] = []
        if key in small and small[key].modified > match.modified:
            codes.append("minimum_size")
        current = newest.get(key)
        if (
            timed_out
            and current
            and (current.path not in ready or current.path != match.path)
        ):
            codes.append("upload_timeout")
        if timed_out and match.path not in ready:
            codes.append("completion_unverified")
        if codes:
            reasons[key] = codes
    if selected:
        overall = max(selected.values(), key=lambda item: item.modified)
        overall_codes = list(reasons.get(result_key(overall.extension), []))
        if (
            any(item.modified > overall.modified for item in small.values())
            and "minimum_size" not in overall_codes
        ):
            overall_codes.append("minimum_size")
        current = max(newest.values(), key=lambda item: item.modified)
        if (
            timed_out
            and (current.path not in ready or current.path != overall.path)
            and "upload_timeout" not in overall_codes
        ):
            overall_codes.append("upload_timeout")
        if overall_codes:
            reasons["Overall"] = overall_codes
    for extension in requested:
        key = result_key(extension)
        if key not in selected:
            reasons[key] = ["no_matching_file"]
    if not selected:
        reasons["Overall"] = ["no_matching_file"]
        if timed_out:
            reasons["Overall"].append("upload_timeout")
    for condition, code in (
        (scan.scan_limit_reached, "scan_limit_reached"),
        (notifications_failed, "notifications_unavailable"),
    ):
        if condition:
            reasons.setdefault("Overall", []).append(code)
    return {
        "status": "no_match" if not selected else "fallback" if reasons else "ok",
        "reasons": reasons,
    }


class _Watcher:
    """One native inotify descriptor, bounded watches, and an async consumer."""

    def __init__(self, loop: asyncio.AbstractEventLoop, wake: asyncio.Event) -> None:
        self.loop = loop
        self.wake = wake
        self.events: dict[str, bool] = {}
        self.observer: Any = None
        self.directories: dict[str, Any] = {}
        self.stopped = False
        self.overflow = False
        self.readiness_invalidated = False
        self.stale: set[str] = set()
        self.failed = False
        self.discovery_requested = False
        self.initial_scanned = False
        self.task: asyncio.Task[None] | None = None

    def start(self, criteria: SearchCriteria) -> None:
        if not sys.platform.startswith("linux"):
            raise OSError("inotify is available only on Linux")
        from asyncinotify import Inotify

        self.criteria = criteria
        self.observer = Inotify()
        try:
            self.sync(watch_directories(criteria))
        except BaseException:
            self.stop()
            raise

    def sync(self, directories: list[str]) -> None:
        """Reconcile only validated directory registrations, without recursion."""
        from asyncinotify import Mask

        if self.observer is None or self.stopped:
            return
        wanted = set(directories)
        invalidated = set(self.stale)
        self.stale.difference_update(invalidated)
        for directory in (self.directories.keys() - wanted) | invalidated:
            if directory not in self.directories:
                continue
            watch = self.directories.pop(directory)
            try:
                self.observer.rm_watch(watch)
            except OSError:
                pass  # Kernel may already have removed a deleted watch.
        mask = (
            Mask.CREATE
            | Mask.MODIFY
            | Mask.CLOSE_WRITE
            | Mask.DELETE
            | Mask.MOVED_FROM
            | Mask.MOVED_TO
            | Mask.MOVE_SELF
            | Mask.DELETE_SELF
            | Mask.ONLYDIR
            | Mask.DONT_FOLLOW
        )
        for directory in wanted - self.directories.keys():
            path = Path(directory)
            if (
                path.is_symlink()
                or path.resolve(strict=True) != path
                or not path.is_dir()
            ):
                continue
            self.directories[directory] = self.observer.add_watch(path, mask)

    async def consume(self) -> None:
        """Coalesce relevant changes; process overflow before optional event paths."""
        try:
            from asyncinotify import Mask

            async for event in self.observer:
                if event.mask & Mask.Q_OVERFLOW:
                    self.overflow = True
                    self.discovery_requested = True
                    self.wake.set()
                    continue
                if event.mask & (Mask.MOVE_SELF | Mask.DELETE_SELF | Mask.IGNORED):
                    self.readiness_invalidated = True
                    if event.watch is not None:
                        self.stale.add(str(event.watch.path))
                    self.discovery_requested = True
                    self.wake.set()
                    continue
                path = event.path
                if path is None:
                    continue
                if event.mask & Mask.ISDIR:
                    self.discovery_requested = True
                    self.wake.set()
                    continue
                if not path.name.lower().startswith(
                    self.criteria.filename_prefix.lower()
                ) or (
                    self.criteria.extensions
                    and path.suffix.lower().lstrip(".") not in self.criteria.extensions
                ):
                    continue
                self.events[str(path)] = bool(event.mask & Mask.CLOSE_WRITE)
                if event.mask & (
                    Mask.CREATE | Mask.MOVED_FROM | Mask.MOVED_TO | Mask.DELETE
                ):
                    self.discovery_requested = True
                self.wake.set()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.notification_failed(err)

    def notification_failed(self, err: Exception) -> None:
        """Discard readiness evidence when the notification stream is uncertain."""
        self.failed = True
        self.events.clear()
        self.readiness_invalidated = True
        self.discovery_requested = True
        self.wake.set()
        _LOGGER.warning(
            "Filesystem notifications failed; using stability checks: %s",
            err,
            exc_info=(type(err), err, err.__traceback__)
            if not isinstance(err, (OSError, ValueError))
            else None,
        )

    def check_consumer(self) -> None:
        """Retrieve unexpected task failures before selecting a fallback."""
        if (
            self.task is not None
            and self.task.done()
            and not self.task.cancelled()
            and not self.failed
        ):
            if (err := self.task.exception()) is not None:
                self.notification_failed(err)

    def stop(self) -> None:
        self.stopped = True
        if self.observer is not None:
            self.observer.close()
            self.observer = None
        self.directories.clear()


async def _finish_executor(hass: Any, function: Any, *args: Any) -> Any:
    """Finish setup/cleanup executor work even across repeated cancellation."""
    job = asyncio.ensure_future(hass.async_add_executor_job(function, *args))
    cancelled = False
    while not job.done():
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        if not job.cancelled():
            job.exception()
        raise asyncio.CancelledError
    return job.result()


async def async_fetch_files(
    hass: Any,
    criteria: SearchCriteria,
    *,
    wait_for_upload: bool,
    timeout: float = 30,
    quiet_period: float = 2,
) -> tuple[list[MatchedFile], FetchStatus]:
    """Wait for uploads, returning marked size-qualified fallbacks at the deadline."""
    if not wait_for_upload:
        scan = await hass.async_add_executor_job(scan_files, criteria)
        selected = _newest(scan.files)
        return list(selected.values()), _status(
            scan, selected, criteria.extensions, False, set()
        )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    wake = asyncio.Event()
    watcher = _Watcher(loop, wake)
    signatures: dict[str, tuple[tuple[int, int, int, int], float]] = {}
    closed: dict[str, tuple[int, int, int, int]] = {}
    initial_old: dict[str, tuple[int, int, int, int]] = {}
    watching = False
    notifications_failed = False
    scan: ScanResult | None = None
    next_discovery = 0.0
    try:
        try:
            await _finish_executor(hass, watcher.start, criteria)
            watching = True
            if watcher.observer is not None:
                watcher.task = asyncio.create_task(watcher.consume())
        except (ImportError, OSError, RuntimeError, ValueError) as err:
            notifications_failed = True
            _LOGGER.warning(
                "Filesystem notifications unavailable; using stability checks: %s", err
            )
        while True:
            full_scan = (
                scan is None
                or watcher.discovery_requested
                or loop.time() >= next_discovery
            )
            wake.clear()
            watcher.discovery_requested = False
            if full_scan:
                scan = await hass.async_add_executor_job(scan_files, criteria)
                next_discovery = loop.time() + 1
                if watching and not watcher.failed:
                    try:
                        await _finish_executor(hass, watcher.sync, scan.directories)
                    except (OSError, RuntimeError, ValueError) as err:
                        notifications_failed = True
                        watcher.notification_failed(err)
            else:
                scan = await hass.async_add_executor_job(refresh_files, criteria, scan)
            watcher.check_consumer()
            now = loop.time()
            events, watcher.events = watcher.events, {}
            if watcher.failed:
                events.clear()
            if watcher.overflow or watcher.readiness_invalidated:
                signatures.clear()
                closed.clear()
                initial_old.clear()
                watcher.overflow = False
                watcher.readiness_invalidated = False
                watcher.initial_scanned = True
            # Only the initial candidate scan may use the mtime-age heuristic.
            if not watcher.initial_scanned:
                watcher.initial_scanned = True
                wall_now_ns = time.time_ns()
                initial_old = {
                    match.path: match.signature
                    for match in scan.files
                    if wall_now_ns - match.mtime_ns >= quiet_period * 1_000_000_000
                }
            for path in events:
                signatures.pop(path, None)
                closed.pop(path, None)
                initial_old.pop(path, None)
            current_paths = {match.path for match in scan.files}
            signatures = {
                path: value
                for path, value in signatures.items()
                if path in current_paths
            }
            closed = {
                path: value for path, value in closed.items() if path in current_paths
            }
            ready: set[str] = set()
            for match in scan.files:
                previous = signatures.get(match.path)
                if previous is None or previous[0] != match.signature:
                    signatures[match.path] = (match.signature, now)
                if events.get(match.path):
                    closed[match.path] = match.signature
                if (
                    closed.get(match.path) == match.signature
                    or initial_old.get(match.path) == match.signature
                    or now - signatures[match.path][1] >= quiet_period
                ):
                    ready.add(match.path)
            newest = _newest(scan.files)
            timed_out = now >= deadline
            if timed_out or (
                newest and all(match.path in ready for match in newest.values())
            ):
                final = await hass.async_add_executor_job(scan_files, criteria)
                watcher.check_consumer()
                old_signatures = {match.path: match.signature for match in scan.files}
                ready = {
                    match.path
                    for match in final.files
                    if match.path in ready
                    and old_signatures.get(match.path) == match.signature
                    and match.path not in watcher.events
                    and not watcher.overflow
                    and not watcher.readiness_invalidated
                }
                final_newest = _newest(final.files)
                timed_out = loop.time() >= deadline
                if timed_out or (
                    final_newest
                    and all(item.path in ready for item in final_newest.values())
                ):
                    selected = (
                        _newest([match for match in final.files if match.path in ready])
                        if timed_out
                        else final_newest
                    )
                    if timed_out:
                        for key, match in final_newest.items():
                            selected.setdefault(key, match)
                    return list(selected.values()), _status(
                        final,
                        selected,
                        criteria.extensions,
                        timed_out,
                        ready,
                        notifications_failed or watcher.failed,
                    )
                scan = final
            next_check = min(
                (
                    signatures[match.path][1] + quiet_period
                    for match in newest.values()
                    if match.path not in ready
                ),
                default=now + 0.5,
            )
            delay = max(0, min(0.5, deadline - loop.time(), next_check - loop.time()))
            try:
                await asyncio.wait_for(wake.wait(), delay)
            except TimeoutError:
                pass
    finally:
        cleanup_cancelled = False
        try:
            if watcher.task is not None:
                watcher.task.cancel()

                # Shield drainage so repeated cancellation cannot abandon the reader.
                async def finish_consumer() -> None:
                    try:
                        await watcher.task
                    except asyncio.CancelledError:
                        pass
                    except Exception as err:
                        watcher.notification_failed(err)

                cleanup = asyncio.create_task(finish_consumer())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                    except Exception as err:
                        watcher.notification_failed(err)
                # Retrieve even an immediately completed drainage task's exception.
                try:
                    cleanup.result()
                except Exception as err:
                    watcher.notification_failed(err)
        finally:
            # The reader has stopped; close its descriptor on its owning loop.
            watcher.stop()
        if cleanup_cancelled:
            raise asyncio.CancelledError
