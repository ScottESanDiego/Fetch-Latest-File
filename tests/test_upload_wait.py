"""Upload readiness tests, including a real Linux writer-close notification."""

import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from support import Assertions

from custom_components.fetch_latest_file import upload_wait
from custom_components.fetch_latest_file.scanner import (
    FileSearchError,
    MatchedFile,
    ScanResult,
    SearchCriteria,
    build_file_results,
    scan_files,
    watch_directories,
)

pytestmark = pytest.mark.asyncio


class TestFiles(Assertions):
    @pytest_asyncio.fixture(autouse=True)
    async def files(self, hass, tmp_path, request):
        self.root = tmp_path
        self.hass = hass
        self.addCleanup = request.addfinalizer

    def criteria(self, **kwargs):
        defaults = dict(
            directory=str(self.root),
            filename_prefix="upload",
            extensions=set(),
            min_size=0,
            allowed_directories=[],
            max_depth=10,
            max_files_to_check=10000,
        )
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
        with patch.object(
            upload_wait._Watcher,
            "start",
            side_effect=OSError("test watcher unavailable"),
        ):
            with self.assertLogs(upload_wait.__name__, "WARNING") as logs:
                result = await upload_wait.async_fetch_files(
                    self.hass,
                    criteria or self.criteria(),
                    wait_for_upload=True,
                    **kwargs,
                )
        self.assertTrue(any("stability checks" in message for message in logs.output))
        return result

    async def test_size_filter_skips_newest_only_affected_extension_is_marked(self):
        old = self.file("upload-old.mp4", b"large enough", modified=10)
        self.file("upload-small.mp4", b"x", modified=20)
        newest = self.file("upload-new.jpg", b"large enough", modified=30)
        files, status = await upload_wait.async_fetch_files(
            self.hass, self.criteria(min_size=5), wait_for_upload=False
        )
        self.assertEqual(
            build_file_results(files), {"Overall": newest, "jpg": newest, "mp4": old}
        )
        self.assertEqual(
            status, {"status": "fallback", "reasons": {"mp4": ["minimum_size"]}}
        )

    async def test_immediate_mode_does_not_create_watcher(self):
        path = self.file("upload.file")
        with patch.object(
            upload_wait._Watcher,
            "start",
            side_effect=AssertionError("unexpected watch"),
        ):
            files, status = await upload_wait.async_fetch_files(
                self.hass, self.criteria(), wait_for_upload=False
            )
        self.assertEqual([match.path for match in files], [path])
        self.assertEqual(status, {"status": "ok", "reasons": {}})

    async def test_existing_file_stability_and_missing_extension_do_not_timeout(self):
        path = self.file("upload.jpg")
        start = time.monotonic()
        files, status = await self.fetch_without_notifications(
            self.criteria(extensions={"jpg", "mp4"}), quiet_period=0.02, timeout=2
        )
        self.assertLess(time.monotonic() - start, 1.2)
        self.assertEqual([match.path for match in files], [path])
        self.assertEqual(status["reasons"]["mp4"], ["no_matching_file"])

    async def test_no_match_and_undersized_timeout_do_not_relax_filters(self):
        self.file("upload-small.jpg", b"x")
        self.file("unrelated.jpg", b"large enough")
        files, status = await self.fetch_without_notifications(
            self.criteria(min_size=5, extensions={"jpg"}),
            quiet_period=0.01,
            timeout=0.04,
        )
        self.assertEqual(files, [])
        self.assertEqual(status["status"], "no_match")
        self.assertIn("upload_timeout", status["reasons"]["Overall"])

    async def test_unfinished_timeout_returns_marked_file(self):
        path = self.file("upload.mp4")
        files, status = await self.fetch_without_notifications(
            quiet_period=10, timeout=0.03
        )
        self.assertEqual([match.path for match in files], [path])
        self.assertEqual(status["status"], "fallback")
        for key in ("mp4", "Overall"):
            self.assertEqual(
                status["reasons"][key][:2], ["upload_timeout", "completion_unverified"]
            )

    async def test_new_arrival_is_detected_without_notifications(self):
        async def arrive():
            await asyncio.sleep(0.05)
            self.file("upload.jpg")

        task = asyncio.create_task(arrive())
        files, status = await self.fetch_without_notifications(
            quiet_period=0.01, timeout=2
        )
        await task
        self.assertEqual([Path(match.path).name for match in files], ["upload.jpg"])
        self.assertEqual(status["status"], "fallback")

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "Linux CLOSE_WRITE semantics"
    )
    async def test_real_writer_close_completes_before_long_quiet_period(self):
        # Keep the handle open until inotify has installed the filesystem watch.
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
            task = asyncio.create_task(
                upload_wait.async_fetch_files(
                    self.hass,
                    self.criteria(),
                    wait_for_upload=True,
                    timeout=3,
                    quiet_period=10,
                )
            )
            self.assertTrue(await self.hass.async_add_executor_job(started.wait, 1))
            await asyncio.sleep(0.05)
            self.assertFalse(task.done(), "open writer must remain pending")
            handle.write(b"finished")
            handle.close()
            files, status = await asyncio.wait_for(task, 1.5)
        self.assertEqual([match.path for match in files], [str(path)])
        self.assertEqual(status["status"], "ok")

    async def test_timeout_keeps_pinned_file_instead_of_older_completed_file(self):
        self.file("upload-old.mp4", modified=10)
        newer = self.root / "upload-new.mp4"
        handle = newer.open("wb")
        self.addCleanup(handle.close)
        handle.write(b"pending")
        handle.flush()
        files, status = await upload_wait.async_fetch_files(
            self.hass,
            self.criteria(),
            wait_for_upload=True,
            timeout=0.3,
            quiet_period=10,
        )
        self.assertEqual([match.path for match in files], [str(newer)])
        self.assertEqual(
            status["reasons"],
            {
                "mp4": ["upload_timeout", "completion_unverified"],
                "Overall": ["upload_timeout", "completion_unverified"],
            },
        )

    async def test_cancellation_waits_for_observer_setup_and_stops_it(self):
        entered, release, stopped = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def start(watcher, criteria):
            entered.set()
            release.wait(2)

        def stop(watcher):
            stopped.set()

        with (
            patch.object(upload_wait._Watcher, "start", start),
            patch.object(upload_wait._Watcher, "stop", stop),
        ):
            task = asyncio.create_task(
                upload_wait.async_fetch_files(
                    self.hass, self.criteria(), wait_for_upload=True
                )
            )
            self.assertTrue(await self.hass.async_add_executor_job(entered.wait, 1))
            task.cancel()
            await asyncio.sleep(0.03)
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

        with (
            patch.object(upload_wait._Watcher, "start", start),
            patch.object(upload_wait._Watcher, "stop", stop),
        ):
            task = asyncio.create_task(
                upload_wait.async_fetch_files(
                    self.hass, self.criteria(), wait_for_upload=True
                )
            )
            self.assertTrue(await self.hass.async_add_executor_job(entered.wait, 1))
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.sleep(0.02)
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

        with (
            patch.object(upload_wait._Watcher, "start"),
            patch.object(upload_wait._Watcher, "sync", sync),
            patch.object(upload_wait._Watcher, "stop", stop),
        ):
            task = asyncio.create_task(
                upload_wait.async_fetch_files(
                    self.hass, self.criteria(), wait_for_upload=True
                )
            )
            self.assertTrue(await self.hass.async_add_executor_job(entered.wait, 1))
            task.cancel()
            await asyncio.sleep(0.02)
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

        def schedule(directory, mask):
            scheduled.append(str(directory))
            return str(directory)

        watcher = upload_wait._Watcher(asyncio.get_running_loop(), asyncio.Event())
        watcher.observer = SimpleNamespace(add_watch=schedule, rm_watch=removed.append)
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
        (self.root / "linked-directory").symlink_to(
            outside.name, target_is_directory=True
        )
        scan = scan_files(self.criteria(max_depth=1))
        self.assertEqual({match.path for match in scan.files}, {top, nested})
        self.assertEqual(
            set(scan.directories), {str(self.root), str(self.root / "child")}
        )
        with self.assertRaises(FileSearchError):
            scan_files(self.criteria(allowed_directories=[outside.name]))
        directories = watch_directories(self.criteria(max_files_to_check=1))
        self.assertLessEqual(len(directories), 1)

    async def test_reserved_extension_cannot_overwrite_fetch_status(self):
        path = self.file("upload.FetchStatus")
        files, status = await upload_wait.async_fetch_files(
            self.hass, self.criteria(), wait_for_upload=False
        )
        self.assertEqual(
            build_file_results(files), {"Overall": path, "ext_fetchstatus": path}
        )


class TestRevalidation(Assertions):
    @pytest_asyncio.fixture(autouse=True)
    async def setup(self, hass):
        self.hass = hass

    async def test_timeout_remains_bounded_when_every_final_scan_changes(self):
        hass = self.hass
        counter = 0

        def scan(criteria):
            nonlocal counter
            counter += 1
            return ScanResult(
                [MatchedFile(counter, "/upload.jpg", "jpg", 10, counter, 1, 1)], [], []
            )

        criteria = SearchCriteria("/unused", "", set(), 0, [], 10, 100)
        with (
            patch.object(upload_wait._Watcher, "start"),
            patch.object(upload_wait._Watcher, "sync"),
            patch.object(upload_wait._Watcher, "stop"),
            patch.object(upload_wait, "scan_files", scan),
            patch.object(
                upload_wait, "refresh_files", lambda criteria, previous: scan(criteria)
            ),
        ):
            files, status = await asyncio.wait_for(
                upload_wait.async_fetch_files(
                    hass, criteria, wait_for_upload=True, timeout=0.03, quiet_period=10
                ),
                0.5,
            )
        self.assertEqual(len(files), 1)
        self.assertIn("completion_unverified", status["reasons"]["Overall"])
        self.assertLess(counter, 10)

    async def test_deletion_during_final_scan_reselects_and_returns_no_match(self):
        hass = self.hass
        candidate = MatchedFile(
            10, "/upload.jpg", "jpg", 10, time.time_ns() + 10000000000, 1, 1
        )
        count = 0

        def scan(criteria):
            nonlocal count
            count += 1
            return ScanResult([candidate] if count < 3 else [], [], [])

        criteria = SearchCriteria("/unused", "", set(), 0, [], 10, 100)
        with (
            patch.object(upload_wait._Watcher, "start"),
            patch.object(upload_wait._Watcher, "sync"),
            patch.object(upload_wait._Watcher, "stop"),
            patch.object(upload_wait, "scan_files", scan),
            patch.object(
                upload_wait, "refresh_files", lambda criteria, previous: scan(criteria)
            ),
        ):
            files, status = await upload_wait.async_fetch_files(
                hass, criteria, wait_for_upload=True, timeout=0.05, quiet_period=0.001
            )
        self.assertEqual(files, [])
        self.assertEqual(status["status"], "no_match")

    async def test_replacement_during_final_scan_never_replaces_pin(self):
        hass = self.hass
        original = MatchedFile(
            10, "/upload.jpg", "jpg", 10, time.time_ns() + 10000000000, 1, 1
        )
        replacement = MatchedFile(
            20, "/upload.jpg", "jpg", 12, time.time_ns() + 10000000000, 1, 2
        )
        count = 0

        def scan(criteria):
            nonlocal count
            count += 1
            return ScanResult([original if count < 3 else replacement], [], [])

        criteria = SearchCriteria("/unused", "", set(), 0, [], 10, 100)
        started = time.monotonic()
        with (
            patch.object(upload_wait._Watcher, "start"),
            patch.object(upload_wait._Watcher, "sync"),
            patch.object(upload_wait._Watcher, "stop"),
            patch.object(upload_wait, "scan_files", scan),
            patch.object(
                upload_wait, "refresh_files", lambda criteria, previous: scan(criteria)
            ),
        ):
            files, status = await upload_wait.async_fetch_files(
                hass, criteria, wait_for_upload=True, timeout=0.3, quiet_period=0.05
            )
        self.assertEqual(files, [])
        self.assertGreaterEqual(time.monotonic() - started, 0.29)
        self.assertEqual(status["status"], "no_match")


@pytest.mark.asyncio
async def test_old_initial_mtime_requires_observed_quiet(hass, tmp_path):
    path = tmp_path / "upload.jpg"
    path.write_bytes(b"complete")
    os.utime(path, (10, 10))
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    started = time.monotonic()
    files, status = await upload_wait.async_fetch_files(
        hass, criteria, wait_for_upload=True, quiet_period=0.05, timeout=1
    )
    assert files[0].path == str(path)
    assert status["status"] == "ok"
    assert time.monotonic() - started >= 0.045


@pytest.mark.parametrize("future", [False, True])
async def test_recent_future_mtime_requires_observed_quiet(hass, tmp_path, future):
    path = tmp_path / "upload.jpg"
    path.write_bytes(b"complete")
    stamp = time.time() + (60 if future else 0)
    os.utime(path, (stamp, stamp))
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    started = time.monotonic()
    files, status = await upload_wait.async_fetch_files(
        hass, criteria, wait_for_upload=True, quiet_period=0.1, timeout=1
    )
    assert files[0].path == str(path)
    assert status["status"] == "ok"
    assert time.monotonic() - started >= 0.09


async def test_unrelated_real_filesystem_noise_does_not_delay_ready_file(
    hass, tmp_path
):
    path = tmp_path / "upload.jpg"
    path.write_bytes(b"complete")
    criteria = SearchCriteria(str(tmp_path), "upload", {"jpg"}, 0, [], 10, 100)
    stop = asyncio.Event()

    async def noise():
        while not stop.is_set():
            (tmp_path / "unrelated.log").write_bytes(b"noise")
            await asyncio.sleep(0.002)

    task = asyncio.create_task(noise())
    started = time.monotonic()
    try:
        files, status = await upload_wait.async_fetch_files(
            hass, criteria, wait_for_upload=True, quiet_period=0.1, timeout=1
        )
    finally:
        stop.set()
        await task
    assert files[0].path == str(path)
    assert status["status"] == "ok"
    assert time.monotonic() - started < 0.5


async def test_extension_keys_are_injective(hass, tmp_path):
    extensions = [
        "",
        "overall",
        "ext_overall",
        "no_extension",
        "ext_no_extension",
        "fetchstatus",
        "jpg",
    ]
    paths = []
    for extension in extensions:
        path = tmp_path / ("upload" + (f".{extension}" if extension else ""))
        path.write_bytes(b"complete")
        paths.append(str(path))
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    files, status = await upload_wait.async_fetch_files(
        hass, criteria, wait_for_upload=False
    )
    results = build_file_results(files)
    assert len(results) == len(extensions) + 1
    assert set(results.values()) == set(paths)
    assert results["no_extension"] == paths[0]
    assert results["ext_ext_overall"] == paths[2]
    assert results["ext_no_extension"] == paths[3]


async def test_scan_limit_diagnostic_and_walk_error(hass, tmp_path):
    for index in range(3):
        (tmp_path / f"upload{index}.jpg").write_bytes(b"complete")
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 1)
    files, status = await upload_wait.async_fetch_files(
        hass, criteria, wait_for_upload=False
    )
    assert len(files) == 1
    assert status["reasons"]["Overall"] == ["scan_limit_reached"]

    def failed_walk(*args, onerror, **kwargs):
        onerror(PermissionError("walk denied"))
        return iter(())

    with patch("custom_components.fetch_latest_file.scanner.os.walk", failed_walk):
        with pytest.raises(PermissionError, match="walk denied"):
            await hass.async_add_executor_job(scan_files, criteria)


@pytest.mark.parametrize("kind", ["overflow", "moved", "deleted", "ignored"])
async def test_pathless_overflow_and_directory_invalidation(kind, tmp_path):
    from asyncinotify import Mask

    mask = {
        "overflow": Mask.Q_OVERFLOW,
        "moved": Mask.MOVE_SELF,
        "deleted": Mask.DELETE_SELF,
        "ignored": Mask.IGNORED,
    }[kind]
    event = SimpleNamespace(mask=mask, path=None, watch=SimpleNamespace(path=tmp_path))

    class Events:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if getattr(self, "delivered", False):
                raise StopAsyncIteration
            self.delivered = True
            return event

    watcher = upload_wait._Watcher(asyncio.get_running_loop(), asyncio.Event())
    watcher.observer = Events()
    await watcher.consume()
    assert watcher.discovery_requested
    assert watcher.wake.is_set()
    if kind == "overflow":
        assert watcher.overflow
    else:
        assert watcher.readiness_invalidated
        assert str(tmp_path) in watcher.stale


@pytest.mark.parametrize("invalid", ["overflow", "readiness_invalidated"])
async def test_invalidated_watch_discards_old_mtime_credit(hass, tmp_path, invalid):
    path = tmp_path / "upload.jpg"
    path.write_bytes(b"complete")
    os.utime(path, (10, 10))
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    original = upload_wait._Watcher.start

    def start(watcher, criteria):
        original(watcher, criteria)
        setattr(watcher, invalid, True)

    started = time.monotonic()
    with patch.object(upload_wait._Watcher, "start", start):
        files, status = await upload_wait.async_fetch_files(
            hass, criteria, wait_for_upload=True, quiet_period=0.1, timeout=1
        )
    assert files[0].path == str(path)
    assert status["status"] == "ok"
    assert time.monotonic() - started >= 0.09


async def test_non_linux_stability_fallback(hass, tmp_path):
    path = tmp_path / "upload.jpg"
    path.write_bytes(b"complete")
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    with patch.object(upload_wait.sys, "platform", "darwin"):
        files, status = await upload_wait.async_fetch_files(
            hass, criteria, wait_for_upload=True, quiet_period=0.05, timeout=1
        )
    assert files[0].path == str(path)
    assert "notifications_unavailable" in status["reasons"]["Overall"]


async def test_consumer_cancel_closes_real_descriptor(hass, tmp_path):
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    created = []
    original = upload_wait._Watcher.start

    def start(watcher, criteria):
        original(watcher, criteria)
        created.append(watcher)

    with patch.object(upload_wait._Watcher, "start", start):
        task = asyncio.create_task(
            upload_wait.async_fetch_files(
                hass, criteria, wait_for_upload=True, timeout=5
            )
        )
        while not created or created[0].task is None:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert created[0].observer is None
    assert created[0].directories == {}
    assert created[0].task.done()


async def test_initial_relevant_event_invalidates_old_age_credit(hass, tmp_path):
    path = tmp_path / "upload.jpg"
    path.write_bytes(b"complete")
    os.utime(path, (10, 10))
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    original = upload_wait._Watcher.start

    def start(watcher, criteria):
        original(watcher, criteria)
        watcher.events[str(path)] = False

    started = time.monotonic()
    with patch.object(upload_wait._Watcher, "start", start):
        files, status = await upload_wait.async_fetch_files(
            hass, criteria, wait_for_upload=True, quiet_period=0.1, timeout=1
        )
    assert files[0].path == str(path)
    assert time.monotonic() - started >= 0.09


async def test_real_directory_move_reconciles_watches(hass, tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    path = child / "upload.jpg"
    handle = path.open("wb")
    handle.write(b"pending")
    handle.flush()
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    created = []
    original = upload_wait._Watcher.start

    def start(watcher, criteria):
        original(watcher, criteria)
        created.append(watcher)

    with patch.object(upload_wait._Watcher, "start", start):
        task = asyncio.create_task(
            upload_wait.async_fetch_files(
                hass, criteria, wait_for_upload=True, quiet_period=10, timeout=2
            )
        )
        try:
            while not created or created[0].task is None:
                await asyncio.sleep(0.01)
            moved = tmp_path / "moved"
            child.rename(moved)
            for _ in range(100):
                await asyncio.sleep(0.01)
                if (
                    str(moved) in created[0].directories
                    and str(child) not in created[0].directories
                ):
                    break
            assert str(moved) in created[0].directories
            assert str(child) not in created[0].directories
            handle.close()
            files, status = await asyncio.wait_for(task, 3)
            assert files == []
            assert status["status"] == "no_match"
        finally:
            handle.close()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task


async def test_repeated_real_waits_do_not_leak_descriptors(hass, tmp_path):
    path = tmp_path / "upload.jpg"
    path.write_bytes(b"complete")
    os.utime(path, (10, 10))
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    # Warm the executor before measuring process descriptors.
    await hass.async_add_executor_job(lambda: None)
    before = await hass.async_add_executor_job(lambda: len(os.listdir("/proc/self/fd")))
    for _ in range(5):
        await upload_wait.async_fetch_files(hass, criteria, wait_for_upload=True)
    after = await hass.async_add_executor_job(lambda: len(os.listdir("/proc/self/fd")))
    assert after == before


async def test_old_candidate_does_not_wait_for_future_arrival(hass, tmp_path):
    path = tmp_path / "upload-old.jpg"
    path.write_bytes(b"complete")
    os.utime(path, (10, 10))
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    arrival = asyncio.Event()

    async def later_upload():
        await arrival.wait()
        (tmp_path / "upload-new.jpg").write_bytes(b"new upload")

    task = asyncio.create_task(later_upload())
    try:
        files, status = await asyncio.wait_for(
            upload_wait.async_fetch_files(
                hass, criteria, wait_for_upload=True, quiet_period=0.05, timeout=2
            ),
            0.5,
        )
        assert files[0].path == str(path)
        assert status["status"] == "ok"
        assert not task.done()
    finally:
        arrival.set()
        await task


@pytest.mark.parametrize("bypass_handler", [False, True])
async def test_unexpected_consumer_failure_returns_fallback_and_closes_on_loop(
    hass, tmp_path, caplog, bypass_handler
):
    from asyncinotify import Mask

    path = tmp_path / "upload.jpg"
    path.write_bytes(b"complete")
    os.utime(path, (10, 10))
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    loop_thread = threading.get_ident()
    created = []
    closed = []
    failure_delivered = threading.Event()
    original_scan = upload_wait.scan_files

    class BrokenEvents:
        delivered = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.delivered:
                self.delivered = True
                return SimpleNamespace(mask=Mask.CLOSE_WRITE, path=path)
            failure_delivered.set()
            raise RuntimeError("unexpected notification failure")

        def close(self):
            assert created[0].task.done()
            closed.append(threading.get_ident())

    def start(watcher, criteria):
        watcher.criteria = criteria
        watcher.observer = BrokenEvents()
        created.append(watcher)

    async def failed_consumer(watcher):
        failure_delivered.set()
        raise RuntimeError("unexpected notification failure")

    def scan(criteria):
        # Ensure the fault occurred, rather than cancelling an unstarted reader.
        assert failure_delivered.wait(1)
        return original_scan(criteria)

    consumer = failed_consumer if bypass_handler else upload_wait._Watcher.consume
    with (
        patch.object(upload_wait._Watcher, "start", start),
        patch.object(upload_wait._Watcher, "sync"),
        patch.object(upload_wait._Watcher, "consume", consumer),
        patch.object(upload_wait, "scan_files", scan),
    ):
        files, status = await upload_wait.async_fetch_files(
            hass, criteria, wait_for_upload=True, timeout=0.08, quiet_period=10
        )
    assert [match.path for match in files] == [str(path)]
    assert status["status"] == "fallback"
    assert status["reasons"]["Overall"] == [
        "upload_timeout",
        "completion_unverified",
        "notifications_unavailable",
    ]
    assert created[0].failed
    assert created[0].observer is None
    assert created[0].task.done()
    assert closed == [loop_thread]
    records = [record for record in caplog.records if record.exc_info]
    assert any(isinstance(record.exc_info[1], RuntimeError) for record in records)


async def test_repeated_cancel_drains_reader_before_loop_thread_close(hass, tmp_path):
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    reading, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lifecycle = []
    loop_thread = threading.get_ident()

    class SlowReader:
        def __aiter__(self):
            return self

        async def __anext__(self):
            reading.set()
            try:
                await asyncio.Event().wait()
            finally:
                draining.set()
                await release.wait()
                lifecycle.append("drained")

        def close(self):
            assert threading.get_ident() == loop_thread
            lifecycle.append("closed")

    def start(watcher, criteria):
        watcher.criteria = criteria
        watcher.observer = SlowReader()

    with (
        patch.object(upload_wait._Watcher, "start", start),
        patch.object(upload_wait._Watcher, "sync"),
    ):
        task = asyncio.create_task(
            upload_wait.async_fetch_files(hass, criteria, wait_for_upload=True)
        )
        await asyncio.wait_for(reading.wait(), 1)
        task.cancel()
        await asyncio.wait_for(draining.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert lifecycle == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    assert lifecycle == ["drained", "closed"]


async def test_watch_sync_failure_discards_old_and_close_readiness(
    hass, tmp_path, caplog
):
    path = tmp_path / "upload.jpg"
    path.write_bytes(b"complete")
    os.utime(path, (10, 10))
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    created = []
    original_sync = upload_wait._Watcher.sync
    original_start = upload_wait._Watcher.start
    sync_calls = 0

    def start(watcher, criteria):
        original_start(watcher, criteria)
        created.append(watcher)

    def sync(watcher, directories):
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls > 1:
            watcher.events[str(path)] = True
            raise OSError("watch reconciliation failed")
        original_sync(watcher, directories)

    started = time.monotonic()
    with (
        patch.object(upload_wait._Watcher, "start", start),
        patch.object(upload_wait._Watcher, "sync", sync),
    ):
        files, status = await upload_wait.async_fetch_files(
            hass, criteria, wait_for_upload=True, timeout=1, quiet_period=0.1
        )
    assert files[0].path == str(path)
    assert time.monotonic() - started >= 0.09
    assert status == {
        "status": "fallback",
        "reasons": {"Overall": ["notifications_unavailable"]},
    }
    assert created[0].failed
    assert created[0].observer is None
    assert any(
        "watch reconciliation failed" in record.message for record in caplog.records
    )


@pytest.mark.parametrize("disappeared", [FileNotFoundError, NotADirectoryError])
async def test_vanished_candidate_and_symlink_cycle_remain_excluded(
    hass, tmp_path, disappeared
):
    vanished = tmp_path / "upload-vanished.jpg"
    vanished.write_bytes(b"gone")
    cycle = tmp_path / "upload-cycle.jpg"
    other = tmp_path / "cycle-target"
    cycle.symlink_to(other)
    other.symlink_to(cycle)
    good = tmp_path / "upload-good.jpg"
    good.write_bytes(b"complete")
    criteria = SearchCriteria(str(tmp_path), "upload", set(), 0, [], 10, 100)
    stat = os.stat

    def vanished_stat(candidate, *args, **kwargs):
        if (
            os.fspath(candidate) == str(vanished)
            and kwargs.get("follow_symlinks") is False
        ):
            raise disappeared("candidate vanished")
        return stat(candidate, *args, **kwargs)

    with patch("custom_components.fetch_latest_file.scanner.os.stat", vanished_stat):
        files, status = await upload_wait.async_fetch_files(
            hass, criteria, wait_for_upload=False
        )
    assert [match.path for match in files] == [str(good)]
    assert status["status"] == "ok"
