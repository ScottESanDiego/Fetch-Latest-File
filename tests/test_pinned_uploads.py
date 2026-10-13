"""Event-image freshness and immutable candidate selection while waiting."""

import asyncio
from unittest.mock import patch

import probatio as vol
import pytest

from custom_components import fetch_latest_file as integration
from custom_components.fetch_latest_file import upload_wait
from custom_components.fetch_latest_file.scanner import (
    MatchedFile,
    ScanResult,
    SearchCriteria,
    build_file_results,
)


def match(name, stamp, *, size=10, inode=1, extension="jpg"):
    return MatchedFile(stamp / 1e9, name, extension, size, stamp, 1, inode)


def criteria(*, extensions=None, min_size=0):
    return SearchCriteria("/unused", "", extensions or {"jpg"}, min_size, [], 10, 100)


async def synthetic_fetch(hass, scans, **kwargs):
    count = 0

    def scan(*_):
        nonlocal count
        snapshot = scans[min(count, len(scans) - 1)]
        count += 1
        return snapshot

    with (
        patch.object(upload_wait._Watcher, "start"),
        patch.object(upload_wait._Watcher, "sync"),
        patch.object(upload_wait._Watcher, "stop"),
        patch.object(upload_wait, "scan_files", scan),
        patch.object(upload_wait, "refresh_files", scan),
    ):
        result = await upload_wait.async_fetch_files(
            hass, criteria=kwargs.pop("criteria", criteria()), **kwargs
        )
    return result, count


def test_fresh_selection_initial_newest_then_first_later_batch_and_fixed_cutoff():
    old = match("old.jpg", 99)
    first = match("b.jpg", 100)
    tie = match("a.jpg", 100)
    newer = match("newer.jpg", 105)
    pins = {}
    scan = ScanResult([old, first, newer], [], [])
    upload_wait._pin_candidates(scan, pins, {"jpg"}, 100, True)
    assert pins["jpg"] == newer
    upload_wait._pin_candidates(
        ScanResult([match("latest.jpg", 200)], [], []), pins, {"jpg"}, 100, False
    )
    assert pins["jpg"] == newer

    pins = {}
    upload_wait._pin_candidates(ScanResult([old], [], []), pins, {"jpg"}, 100, True)
    assert pins == {}
    upload_wait._pin_candidates(
        ScanResult([old, first, tie, newer], [], []), pins, {"jpg"}, 100, False
    )
    assert pins["jpg"] == tie


@pytest.mark.asyncio
async def test_initial_recent_complete_and_later_arrivals_never_replace_pin(hass):
    first = match("first.jpg", 100)
    later = match("later.jpg", 101)
    (files, status), _ = await synthetic_fetch(
        hass,
        [ScanResult([first], [], []), ScanResult([first, later], [], [])],
        wait_for_upload=True,
        fresh_extensions={"jpg"},
        invocation_time_ns=100,
        freshness_window=1e-9,
        quiet_period=0.01,
        timeout=1,
    )
    assert files == [first]
    assert status == {"status": "ok", "reasons": {}}


@pytest.mark.asyncio
async def test_wait_for_missing_fresh_image_despite_ready_existing_video(hass):
    video = match("video.mp4", 1, extension="mp4")
    image = match("image.jpg", 100)
    scans = [ScanResult([video], [], [])] * 4
    scans += [ScanResult([video, image], [], [])]
    (files, status), count = await synthetic_fetch(
        hass,
        scans,
        criteria=criteria(extensions={"jpg", "mp4"}),
        wait_for_upload=True,
        fresh_extensions={"jpg"},
        invocation_time_ns=100,
        freshness_window=1e-9,
        quiet_period=0.005,
        timeout=3,
    )
    assert count >= 5
    assert build_file_results(files)["jpg"] == "image.jpg"
    assert status["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_extension", ["jpg", "mp4"])
async def test_pinned_partial_upload_grows_before_return(hass, partial_extension):
    partial = match("partial", 100, size=1, extension=partial_extension)
    grown = match("partial", 101, size=10, extension=partial_extension)
    other_extension = "mp4" if partial_extension == "jpg" else "jpg"
    other = match("other", 100, extension=other_extension)
    newer = match("newer", 102, extension=partial_extension)
    (files, status), count = await synthetic_fetch(
        hass,
        [ScanResult([other], [partial], [])] * 3
        + [ScanResult([other, grown, newer], [], [])],
        criteria=criteria(extensions={"jpg", "mp4"}, min_size=5),
        wait_for_upload=True,
        fresh_extensions={"jpg"},
        invocation_time_ns=100,
        freshness_window=1e-9,
        quiet_period=0.005,
        timeout=3,
    )
    assert count >= 4
    assert grown in files
    assert newer not in files
    assert status["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "replaced", "undersized"])
async def test_pinned_file_never_substituted_when_unavailable(hass, change):
    pinned = match("pin.jpg", 100)
    later = match("later.jpg", 101)
    current = [] if change == "deleted" else [match("pin.jpg", 100, inode=2)]
    small = [match("pin.jpg", 100, size=1)] if change == "undersized" else []
    if change == "undersized":
        current = []
    (files, status), _ = await synthetic_fetch(
        hass,
        [ScanResult([pinned], [], []), ScanResult([later, *current], small, [])],
        criteria=criteria(min_size=5),
        wait_for_upload=True,
        fresh_extensions={"jpg"},
        invocation_time_ns=100,
        freshness_window=1e-9,
        quiet_period=1,
        timeout=0.02,
    )
    assert files == []
    assert status["status"] == "no_match"
    assert status["reasons"]["jpg"] == ["no_matching_file", "upload_timeout"]


@pytest.mark.asyncio
async def test_timeout_returns_same_incomplete_pin_not_completed_older_file(hass):
    older = match("old.jpg", 1)
    pinned = match("pin.jpg", 100)
    (files, status), _ = await synthetic_fetch(
        hass,
        [ScanResult([older, pinned], [], [])],
        wait_for_upload=True,
        quiet_period=1,
        timeout=0.02,
    )
    assert files == [pinned]
    assert status["reasons"]["jpg"] == ["upload_timeout", "completion_unverified"]


@pytest.mark.asyncio
async def test_no_wait_ignores_freshness_and_preserves_size_filter(hass):
    older = match("old.jpg", 1)
    small = match("small.jpg", 100, size=1)
    (files, _), _ = await synthetic_fetch(
        hass,
        [ScanResult([older], [small], [])],
        criteria=criteria(min_size=5),
        wait_for_upload=False,
        fresh_extensions={"jpg"},
        invocation_time_ns=1000,
        freshness_window=1e-9,
    )
    assert files == [older]


def test_schema_fresh_extensions_and_window():
    schema = integration.SERVICE_FETCH_SCHEMA
    base = {"directory": "/tmp", "filename": "", "extension": [".JPG", "MP4"]}
    assert schema(base)["freshness_window"] == 5
    assert schema(base)["fresh_extensions"] == []
    assert schema({**base, "fresh_extensions": [".JPG", "jpg"]})[
        "fresh_extensions"
    ] == ["jpg"]
    with pytest.raises(vol.Invalid, match="must be included in extension"):
        schema({**base, "fresh_extensions": ["png"]})
    with pytest.raises(vol.Invalid):
        schema({"directory": "/tmp", "filename": "", "fresh_extensions": ["jpg"]})
    for window in [0, -1, "nan", "inf", "invalid"]:
        with pytest.raises(vol.Invalid):
            schema({**base, "freshness_window": window})


def test_previously_excluded_updated_file_can_be_first_eligible_candidate():
    pins = {}
    old = match("same.jpg", 99)
    upload_wait._pin_candidates(ScanResult([old], [], []), pins, {"jpg"}, 100, True)
    assert pins == {}
    updated = match("same.jpg", 100)
    upload_wait._pin_candidates(
        ScanResult([updated], [], []), pins, {"jpg"}, 100, False
    )
    assert pins["jpg"] == updated


@pytest.mark.asyncio
async def test_missing_fresh_image_times_out_even_when_video_ready(hass):
    video = match("video.mp4", 1, extension="mp4")
    (files, status), _ = await synthetic_fetch(
        hass,
        [ScanResult([video], [], [])],
        criteria=criteria(extensions={"jpg", "mp4"}),
        wait_for_upload=True,
        fresh_extensions={"jpg"},
        invocation_time_ns=100,
        freshness_window=1e-9,
        quiet_period=0.001,
        timeout=0.02,
    )
    assert files == [video]
    assert status["status"] == "fallback"
    assert status["reasons"]["jpg"] == ["no_matching_file", "upload_timeout"]
    assert "mp4" not in status["reasons"]


@pytest.mark.asyncio
async def test_first_fresh_candidate_discovered_on_final_scan_can_be_pinned(hass):
    image = match("image.jpg", 100)
    (files, status), _ = await synthetic_fetch(
        hass,
        [ScanResult([], [], [])] * 2 + [ScanResult([image], [], [])],
        wait_for_upload=True,
        fresh_extensions={"jpg"},
        invocation_time_ns=100,
        freshness_window=1e-9,
        quiet_period=1,
        timeout=0.02,
    )
    assert files == [image]
    assert status["reasons"]["jpg"] == ["upload_timeout", "completion_unverified"]


@pytest.mark.asyncio
async def test_service_invocation_boundary_precedes_target_lock(hass, entry, tmp_path):
    runtime = entry.runtime_data
    clock = [100]
    received = []

    async def fetch(*args, **kwargs):
        received.append(kwargs["invocation_time_ns"])
        return [], {"status": "no_match", "reasons": {"Overall": ["no_matching_file"]}}

    with (
        patch.object(integration.time, "time_ns", side_effect=lambda: clock[0]),
        patch.object(integration, "async_fetch_files", fetch),
    ):
        async with runtime.operation("default"):
            task = asyncio.create_task(
                hass.services.async_call(
                    "fetch_latest_file",
                    "fetch",
                    {"directory": str(tmp_path), "filename": ""},
                    blocking=True,
                    return_response=True,
                )
            )
            for _ in range(20):
                await asyncio.sleep(0)
                if runtime.locks["default"].references == 2:
                    break
            assert runtime.locks["default"].references == 2
            assert not task.done()
            clock[0] = 200
        await task
    assert received == [100]
