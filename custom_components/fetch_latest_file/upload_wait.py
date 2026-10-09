"""Notification-driven upload readiness with bounded, graceful fallback."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from .scanner import MatchedFile, ScanResult, SearchCriteria, result_key, scan_files, watch_directories

_LOGGER = logging.getLogger(__name__)


def _newest(files: list[MatchedFile]) -> dict[str, MatchedFile]:
    selected: dict[str, MatchedFile] = {}
    for match in sorted(files, key=lambda item: item.modified, reverse=True):
        selected.setdefault(result_key(match.extension), match)
    return selected


def _status(scan: ScanResult, selected: dict[str, MatchedFile], requested: set[str],
            timed_out: bool, ready: set[str]) -> dict[str, Any]:
    reasons: dict[str, list[str]] = {}
    for key, match in selected.items():
        codes = []
        if any(result_key(small.extension) == key and small.modified > match.modified
               for small in scan.undersized):
            codes.append("minimum_size")
        newest = _newest(scan.files).get(key)
        if timed_out and newest and (newest.path not in ready or newest.path != match.path):
            codes.append("upload_timeout")
        if timed_out and match.path not in ready:
            codes.append("completion_unverified")
        if codes:
            reasons[key] = codes
    if selected:
        overall = max(selected.values(), key=lambda item: item.modified)
        overall_codes = list(reasons.get(result_key(overall.extension), []))
        if any(small.modified > overall.modified for small in scan.undersized):
            if "minimum_size" not in overall_codes:
                overall_codes.append("minimum_size")
        newest_overall = max(scan.files, key=lambda item: item.modified)
        if timed_out and (newest_overall.path not in ready or newest_overall.path != overall.path):
            if "upload_timeout" not in overall_codes:
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
    return {"status": "no_match" if not selected else "fallback" if reasons else "ok",
            "reasons": reasons}


class _Watcher:
    """Executor-owned watchdog observer; loop-owned event queue."""

    def __init__(self, loop: asyncio.AbstractEventLoop, wake: asyncio.Event) -> None:
        self.loop = loop
        self.wake = wake
        self.events: list[tuple[str, tuple[int, int, int, int] | None]] = []
        self.observer: Any = None
        self.directories: dict[str, Any] = {}
        self.stopped = False

    def _receive(self, path: str, signature: tuple[int, int, int, int] | None) -> None:
        if not self.stopped:
            self.events.append((path, signature))
            self.wake.set()

    def start(self, criteria: SearchCriteria) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        watcher = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event: Any) -> None:
                # Read-open/read-close events from scans must not reset readiness.
                if event.event_type not in {"created", "modified", "deleted", "moved", "closed"}:
                    return
                signature = None
                if event.event_type == "closed" and not event.is_directory:
                    try:
                        stats = os.stat(event.src_path, follow_symlinks=False)
                        signature = (stats.st_dev, stats.st_ino, stats.st_size, stats.st_mtime_ns)
                    except OSError:
                        pass
                watcher.loop.call_soon_threadsafe(watcher._receive, str(event.src_path), signature)
                destination = getattr(event, "dest_path", None)
                if destination:
                    watcher.loop.call_soon_threadsafe(watcher._receive, str(destination), None)

        self.observer = Observer()
        self.handler = Handler()
        try:
            self.sync(watch_directories(criteria))
            self.observer.start()
        except BaseException:
            self.stop()
            raise

    def sync(self, directories: list[str]) -> None:
        """Watch only scan-validated directories, each nonrecursively."""
        wanted = set(directories)
        for directory in self.directories.keys() - wanted:
            self.observer.unschedule(self.directories.pop(directory))
        for directory in wanted - self.directories.keys():
            path = Path(directory)
            if path.is_symlink() or path.resolve(strict=True) != path or not path.is_dir():
                continue
            self.directories[directory] = self.observer.schedule(
                self.handler, directory, recursive=False)

    def stop(self) -> None:
        self.stopped = True
        if self.observer is not None:
            self.observer.stop()
            if self.observer.ident is not None:
                self.observer.join()


async def _finish_executor(hass: Any, function: Any, *args: Any) -> Any:
    """Let a setup/cleanup executor finish even if its caller is cancelled."""
    job = asyncio.ensure_future(hass.async_add_executor_job(function, *args))
    cancelled = False
    while not job.done():
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            # Repeated automation/unload cancellation must not abandon the thread.
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        # Retrieve an exception before rethrowing cancellation to avoid orphan warnings.
        if not job.cancelled():
            job.exception()
        raise asyncio.CancelledError
    return job.result()


async def async_fetch_files(hass: Any, criteria: SearchCriteria, *, wait_for_upload: bool,
                           timeout: float = 30, quiet_period: float = 2
                           ) -> tuple[list[MatchedFile], dict[str, Any]]:
    """Select current files, waiting optionally and returning marked timeout fallbacks."""
    if not wait_for_upload:
        scan = await hass.async_add_executor_job(scan_files, criteria)
        selected = _newest(scan.files)
        return list(selected.values()), _status(scan, selected, criteria.extensions, False, set())

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    wake = asyncio.Event()
    watcher = _Watcher(loop, wake)
    signatures: dict[str, tuple[tuple[int, int, int, int], float]] = {}
    closed: dict[str, tuple[int, int, int, int]] = {}
    watching = False
    try:
        try:
            await _finish_executor(hass, watcher.start, criteria)
            watching = True
        except (ImportError, OSError, RuntimeError) as err:
            _LOGGER.warning("Filesystem notifications unavailable; using stability checks: %s", err)
        while True:
            wake.clear()
            scan = await hass.async_add_executor_job(scan_files, criteria)
            if watching:
                try:
                    await _finish_executor(hass, watcher.sync, scan.directories)
                except (OSError, RuntimeError) as err:
                    _LOGGER.warning("Could not extend filesystem watches; using periodic scans: %s", err)
            now = loop.time()
            events, watcher.events = watcher.events, []
            for path, signature in events:
                signatures.pop(path, None)
                closed.pop(path, None)
                if signature is not None:
                    closed[path] = signature
            current_paths = {match.path for match in scan.files}
            signatures = {path: value for path, value in signatures.items() if path in current_paths}
            closed = {path: value for path, value in closed.items() if path in current_paths}
            ready: set[str] = set()
            for match in scan.files:
                previous = signatures.get(match.path)
                if previous is None or previous[0] != match.signature:
                    signatures[match.path] = (match.signature, now)
                if (closed.get(match.path) == match.signature
                        or now - signatures[match.path][1] >= quiet_period):
                    ready.add(match.path)
            newest = _newest(scan.files)
            timed_out = now >= deadline
            if timed_out or (newest and all(match.path in ready for match in newest.values())):
                # Revalidate paths and newest selection after readiness, before publication.
                final = await hass.async_add_executor_job(scan_files, criteria)
                if watcher.events or final.files != scan.files or final.undersized != scan.undersized:
                    if not timed_out:
                        continue
                    # Deadline is firm even when writers keep changing files.
                    changed_paths = {path for path, _ in watcher.events}
                    old_signatures = {match.path: match.signature for match in scan.files}
                    ready = {match.path for match in final.files
                             if match.path in ready and match.path not in changed_paths
                             and old_signatures.get(match.path) == match.signature}
                    scan = final
                    newest = _newest(scan.files)
                if timed_out:
                    selected = _newest([match for match in scan.files if match.path in ready])
                    for key, match in newest.items():
                        selected.setdefault(key, match)
                else:
                    selected = newest
                return list(selected.values()), _status(scan, selected, criteria.extensions, timed_out, ready)
            next_check = min(
                (signatures[match.path][1] + quiet_period
                 for match in newest.values() if match.path not in ready),
                default=now + 0.5,
            )
            delay = max(0, min(0.5, deadline - loop.time(), next_check - loop.time()))
            try:
                await asyncio.wait_for(wake.wait(), delay)
            except TimeoutError:
                pass
    finally:
        await _finish_executor(hass, watcher.stop)
