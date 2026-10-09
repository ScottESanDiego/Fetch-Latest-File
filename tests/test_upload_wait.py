"""Upload readiness tests, including a real Linux writer-close notification."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from support import AsyncCase, Hass
from custom_components.fetch_latest_file import upload_wait
from custom_components.fetch_latest_file.scanner import (
    FileSearchError, MatchedFile, ScanResult, SearchCriteria, build_file_results,
    scan_files, watch_directories,
)


class FileCase(AsyncCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hass = Hass()
        self.addCleanup(self.hass.executor.shutdown)

    def criteria(self, **kwargs):
        defaults = dict(directory=str(self.root), filename_prefix="upload", extensions=set(),
                        min_size=0, allowed_directories=[], max_depth=10, max_files_to_check=10000)
        defaults.update(kwargs)
        return SearchCriteria(**defaults)

    def file(self, name, data=b"complete", modified=None):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if modified is not None:
            os.utime(path, (modified, modified))
        return str(path)

    async def fetch_without_notifications(self, criteria=None, **kwargs):
        with patch.object(upload_wait._Watcher, "start", side_effect=OSError("test watcher unavailable")):
            with self.assertLogs(upload_wait.__name__, "WARNING") as logs:
                result = await upload_wait.async_fetch_files(
                    self.hass, criteria or self.criteria(), wait_for_upload=True, **kwargs)
        self.assertTrue(any("stability checks" in message for message in logs.output))
        return result

    async def test_size_filter_skips_newest_only_affected_extension_is_marked(self):
        old = self.file("upload-old.mp4", b"large enough", modified=10)
        self.file("upload-small.mp4", b"x", modified=20)
        newest = self.file("upload-new.jpg", b"large enough", modified=30)
        files, status = await upload_wait.async_fetch_files(
            self.hass, self.criteria(min_size=5), wait_for_upload=False)
        self.assertEqual(build_file_results(files), {"Overall": newest, "jpg": newest, "mp4": old})
        self.assertEqual(status, {"status": "fallback", "reasons": {"mp4": ["minimum_size"]}})

    async def test_immediate_mode_does_not_create_watcher(self):
        path = self.file("upload.file")
        with patch.object(upload_wait._Watcher, "start", side_effect=AssertionError("unexpected watch")):
            files, status = await upload_wait.async_fetch_files(self.hass, self.criteria(), wait_for_upload=False)
        self.assertEqual([match.path for match in files], [path])
        self.assertEqual(status, {"status": "ok", "reasons": {}})

    async def test_existing_file_stability_and_missing_extension_do_not_timeout(self):
        path = self.file("upload.jpg")
        start = time.monotonic()
        files, status = await self.fetch_without_notifications(
            self.criteria(extensions={"jpg", "mp4"}), quiet_period=.02, timeout=2)
        self.assertLess(time.monotonic() - start, 1.2)
        self.assertEqual([match.path for match in files], [path])
        self.assertEqual(status["reasons"], {"mp4": ["no_matching_file"]})

    async def test_no_match_and_undersized_timeout_do_not_relax_filters(self):
        self.file("upload-small.jpg", b"x")
        self.file("unrelated.jpg", b"large enough")
        files, status = await self.fetch_without_notifications(
            self.criteria(min_size=5, extensions={"jpg"}), quiet_period=.01, timeout=.04)
        self.assertEqual(files, [])
        self.assertEqual(status["status"], "no_match")
        self.assertIn("upload_timeout", status["reasons"]["Overall"])

    async def test_unfinished_timeout_returns_marked_file(self):
        path = self.file("upload.mp4")
        files, status = await self.fetch_without_notifications(quiet_period=10, timeout=.03)
        self.assertEqual([match.path for match in files], [path])
        self.assertEqual(status["status"], "fallback")
        for key in ("mp4", "Overall"):
            self.assertEqual(status["reasons"][key], ["upload_timeout", "completion_unverified"])

    async def test_new_arrival_is_detected_without_notifications(self):
        async def arrive():
            await asyncio.sleep(.05)
            self.file("upload.jpg")
        task = asyncio.create_task(arrive())
        files, status = await self.fetch_without_notifications(quiet_period=.01, timeout=2)
        await task
        self.assertEqual([Path(match.path).name for match in files], ["upload.jpg"])
        self.assertEqual(status["status"], "ok")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux CLOSE_WRITE semantics")
    async def test_real_writer_close_completes_before_long_quiet_period(self):
        # Keep the handle open until watchdog has installed the filesystem watch.
        path = self.root / "upload.mp4"
        handle = path.open("wb")
        self.addCleanup(handle.close)
        handle.write(b"partial")
        handle.flush()
        started = threading.Event()
        original_start = upload_wait._Watcher.start

        def start(watcher, criteria):
            original_start(watcher, criteria)
            started.set()

        with patch.object(upload_wait._Watcher, "start", start):
            task = asyncio.create_task(upload_wait.async_fetch_files(
                self.hass, self.criteria(), wait_for_upload=True, timeout=3, quiet_period=10))
            self.assertTrue(await self.hass.async_add_executor_job(started.wait, 1))
            await asyncio.sleep(.05)
            self.assertFalse(task.done(), "open writer must remain pending")
            handle.write(b"finished")
            handle.close()
            files, status = await asyncio.wait_for(task, 1.5)
        self.assertEqual([match.path for match in files], [str(path)])
        self.assertEqual(status["status"], "ok")

    async def test_timeout_uses_older_completed_file(self):
        older = self.file("upload-old.mp4", modified=10)
        started = threading.Event()
        original_start = upload_wait._Watcher.start

        def start(watcher, criteria):
            original_start(watcher, criteria)
            started.set()

        with patch.object(upload_wait._Watcher, "start", start):
            task = asyncio.create_task(upload_wait.async_fetch_files(
                self.hass, self.criteria(), wait_for_upload=True, timeout=.3, quiet_period=10))
            self.assertTrue(await self.hass.async_add_executor_job(started.wait, 1))
            # Notify closure of the older candidate, then introduce a newer open upload.
            with open(older, "ab") as handle:
                handle.write(b"more")
            newer = self.root / "upload-new.mp4"
            handle = newer.open("wb")
            self.addCleanup(handle.close)
            handle.write(b"pending")
            handle.flush()
            files, status = await task
        self.assertEqual([match.path for match in files], [older])
        self.assertEqual(status["reasons"], {"mp4": ["upload_timeout"], "Overall": ["upload_timeout"]})

    async def test_cancellation_waits_for_observer_setup_and_stops_it(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        def start(watcher, criteria):
            entered.set()
            release.wait(2)
        def stop(watcher):
            stopped.set()
        with patch.object(upload_wait._Watcher, "start", start), patch.object(upload_wait._Watcher, "stop", stop):
            task = asyncio.create_task(upload_wait.async_fetch_files(
                self.hass, self.criteria(), wait_for_upload=True))
            self.assertTrue(await self.hass.async_add_executor_job(entered.wait, 1))
            task.cancel()
            await asyncio.sleep(.03)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stopped.is_set())

    async def test_repeated_cancellation_does_not_abandon_startup_thread(self):
        entered, release = threading.Event(), threading.Event()
        lifecycle = []
        def start(watcher, criteria):
            entered.set()
            release.wait(2)
            lifecycle.append("started")
        def stop(watcher):
            lifecycle.append("stopped")
        with patch.object(upload_wait._Watcher, "start", start), patch.object(upload_wait._Watcher, "stop", stop):
            task = asyncio.create_task(upload_wait.async_fetch_files(
                self.hass, self.criteria(), wait_for_upload=True))
            self.assertTrue(await self.hass.async_add_executor_job(entered.wait, 1))
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            self.assertEqual(lifecycle, [])
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertEqual(lifecycle, ["started", "stopped"])

    async def test_cancel_during_watch_sync_finishes_sync_before_stop(self):
        self.file("upload.jpg")
        entered, release = threading.Event(), threading.Event()
        lifecycle = []
        def sync(watcher, directories):
            entered.set()
            release.wait(2)
            lifecycle.append("synced")
        def stop(watcher):
            lifecycle.append("stopped")
        with patch.object(upload_wait._Watcher, "start"), patch.object(upload_wait._Watcher, "sync", sync), \
             patch.object(upload_wait._Watcher, "stop", stop):
            task = asyncio.create_task(upload_wait.async_fetch_files(
                self.hass, self.criteria(), wait_for_upload=True))
            self.assertTrue(await self.hass.async_add_executor_job(entered.wait, 1))
            task.cancel()
            await asyncio.sleep(.02)
            self.assertEqual(lifecycle, [])
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertEqual(lifecycle, ["synced", "stopped"])

    async def test_directory_watch_reconciliation_removes_stale_registrations(self):
        first, second = self.root / "first", self.root / "second"
        first.mkdir()
        second.mkdir()
        scheduled, removed = [], []
        def schedule(handler, directory, recursive):
            self.assertFalse(recursive)
            scheduled.append(directory)
            return directory
        watcher = upload_wait._Watcher(asyncio.get_running_loop(), asyncio.Event())
        watcher.observer = SimpleNamespace(schedule=schedule, unschedule=removed.append)
        watcher.handler = object()
        watcher.sync([str(first)])
        watcher.sync([str(second)])
        self.assertEqual(scheduled, [str(first), str(second)])
        self.assertEqual(removed, [str(first)])
        self.assertEqual(set(watcher.directories), {str(second)})
        watcher.sync([])
        self.assertEqual(removed, [str(first), str(second)])
        self.assertEqual(len(watcher.directories), 0)

    async def test_scanner_security_depth_and_watch_count_limits(self):
        top = self.file("upload.jpg")
        nested = self.file("child/upload.jpg")
        self.file("child/deeper/upload.jpg")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        external = Path(outside.name) / "upload.jpg"
        external.write_bytes(b"outside")
        (self.root / "upload-link.jpg").symlink_to(external)
        (self.root / "linked-directory").symlink_to(outside.name, target_is_directory=True)
        scan = scan_files(self.criteria(max_depth=1))
        self.assertEqual({match.path for match in scan.files}, {top, nested})
        self.assertEqual(set(scan.directories), {str(self.root), str(self.root / "child")})
        with self.assertRaises(FileSearchError):
            scan_files(self.criteria(allowed_directories=[outside.name]))
        directories = watch_directories(self.criteria(max_files_to_check=1))
        self.assertLessEqual(len(directories), 1)

    async def test_reserved_extension_cannot_overwrite_fetch_status(self):
        path = self.file("upload.FetchStatus")
        files, status = await upload_wait.async_fetch_files(self.hass, self.criteria(), wait_for_upload=False)
        self.assertEqual(build_file_results(files), {"Overall": path, "ext_fetchstatus": path})


class RevalidationTests(AsyncCase):
    async def test_timeout_remains_bounded_when_every_final_scan_changes(self):
        hass = Hass()
        self.addCleanup(hass.executor.shutdown)
        counter = 0
        def scan(criteria):
            nonlocal counter
            counter += 1
            return ScanResult([MatchedFile(counter, "/upload.jpg", "jpg", 10, counter, 1, 1)], [], [])
        criteria = SearchCriteria("/unused", "", set(), 0, [], 10, 100)
        with patch.object(upload_wait._Watcher, "start"), patch.object(upload_wait._Watcher, "sync"), \
             patch.object(upload_wait._Watcher, "stop"), patch.object(upload_wait, "scan_files", scan):
            files, status = await asyncio.wait_for(upload_wait.async_fetch_files(
                hass, criteria, wait_for_upload=True, timeout=.03, quiet_period=10), .5)
        self.assertEqual(len(files), 1)
        self.assertIn("completion_unverified", status["reasons"]["Overall"])
        self.assertLess(counter, 10)

    async def test_deletion_during_final_scan_reselects_and_returns_no_match(self):
        hass = Hass()
        self.addCleanup(hass.executor.shutdown)
        candidate = MatchedFile(10, "/upload.jpg", "jpg", 10, 10, 1, 1)
        count = 0
        def scan(criteria):
            nonlocal count
            count += 1
            return ScanResult([candidate] if count < 3 else [], [], [])
        criteria = SearchCriteria("/unused", "", set(), 0, [], 10, 100)
        with patch.object(upload_wait._Watcher, "start"), patch.object(upload_wait._Watcher, "sync"), \
             patch.object(upload_wait._Watcher, "stop"), patch.object(upload_wait, "scan_files", scan):
            files, status = await upload_wait.async_fetch_files(
                hass, criteria, wait_for_upload=True, timeout=.05, quiet_period=.001)
        self.assertEqual(files, [])
        self.assertEqual(status["status"], "no_match")

    async def test_replacement_during_final_scan_loses_old_readiness(self):
        hass = Hass()
        self.addCleanup(hass.executor.shutdown)
        original = MatchedFile(10, "/upload.jpg", "jpg", 10, 10, 1, 1)
        replacement = MatchedFile(20, "/upload.jpg", "jpg", 12, 20, 1, 2)
        count = 0
        def scan(criteria):
            nonlocal count
            count += 1
            return ScanResult([original if count < 3 else replacement], [], [])
        criteria = SearchCriteria("/unused", "", set(), 0, [], 10, 100)
        started = time.monotonic()
        with patch.object(upload_wait._Watcher, "start"), patch.object(upload_wait._Watcher, "sync"), \
             patch.object(upload_wait._Watcher, "stop"), patch.object(upload_wait, "scan_files", scan):
            files, status = await upload_wait.async_fetch_files(
                hass, criteria, wait_for_upload=True, timeout=.3, quiet_period=.05)
        self.assertEqual(files, [replacement])
        self.assertGreaterEqual(time.monotonic() - started, .09)
        self.assertEqual(status["status"], "ok")
