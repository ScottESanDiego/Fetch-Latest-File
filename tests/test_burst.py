"""Upload-cluster ordinals, identity preservation, and same-cluster fallback."""

import asyncio
import contextlib
import threading
import time
from unittest.mock import patch

import probatio as vol
import pytest

from custom_components import fetch_latest_file as integration
from custom_components.fetch_latest_file import upload_wait
from custom_components.fetch_latest_file.burst import BurstTracker, seconds_ns
from custom_components.fetch_latest_file.scanner import (
    MatchedFile,
    ScanResult,
    SearchCriteria,
)


def image(name, stamp, *, size=10, inode=1, extension="webp"):
    return MatchedFile(stamp, name, extension, size, int(stamp * 1e9), 1, inode)


def scan(*matches, min_size=5, limited=False):
    return ScanResult(
        [match for match in matches if match.size >= min_size],
        [match for match in matches if match.size < min_size],
        [],
        limited,
    )


def tracker(position=1, *, gap=5, lookback=60, freshness=5):
    return BurstTracker(
        "webp", position, gap, seconds_ns(100 - lookback), seconds_ns(100 - freshness)
    )


def select(state, snapshot, *, now=100, wall=100, timeout=False):
    return state.select(
        snapshot, now=now, wall_now_ns=seconds_ns(wall), timed_out=timeout
    )


@pytest.mark.parametrize("position", [1, 2, 3])
@pytest.mark.parametrize("camera", ["North", "Front", "East"])
def test_retained_sequences_choose_requested_position(camera, position):
    names, stamps = {
        "North": (
            ["114718", "114721", "114723", "114725", "114727"],
            [88.821643, 90.953664, 93.353689, 95.557711, 97.816734],
        ),
        "Front": (
            ["124758", "124801", "124803", "124805", "124807", "124809", "124811"],
            [
                83.674980,
                85.305997,
                87.392018,
                89.664041,
                91.823063,
                93.829083,
                96.363109,
            ],
        ),
        "East": (["125029", "125032", "125034"], [101.090170, 103.183191, 105.611215]),
    }[camera]
    matches = [image(name, stamp) for name, stamp in zip(names, stamps, strict=True)]
    state = tracker(position)
    if camera == "East":
        assert select(state, scan(image("previous", -75.336))) is None
    assert (
        select(state, scan(image("previous", stamps[0] - 20), *matches))
        == matches[position - 1]
    )


@pytest.mark.parametrize(
    "gap,expected", [(4.999, "first"), (5, "second"), (5.001, "second")]
)
def test_exact_gap_starts_new_cluster(gap, expected):
    first, second = image("first", 90), image("second", 90 + gap)
    assert select(tracker(freshness=10), scan(first, second)).path == expected


def test_fractional_nanosecond_gap_never_splits_a_shorter_integer_interval():
    first, second = image("first", 95), image("second", 100)
    state = tracker(gap=5.0000000001)
    assert select(state, scan(first, second)) == first
    assert state.gap_ns == 5_000_000_001


def test_decimal_gap_exact_boundary_splits_without_binary_float_rounding():
    first = image("first", 95)
    second = MatchedFile(100.2, "second", "webp", 10, 100_200_000_000, 1, 2)
    state = tracker(gap=5.2)
    assert select(state, scan(first, second)) == second
    assert state.gap_ns == 5_200_000_000


def test_positive_subnanosecond_gap_does_not_become_zero():
    state = tracker(gap=1e-10)
    assert state.gap_ns == 1
    first, second = image("a", 99), image("b", 99)
    assert select(state, scan(first, second)) == first


def test_pending_n_waits_and_frozen_order_survives_first_upload_writes():
    first = image("first", 99, size=1)
    state = tracker(2)
    assert select(state, scan(first)) is None
    grown = image("first", 103, size=10)
    second = image("second", 102)
    assert select(state, scan(grown, second), now=103, wall=103) == second
    assert [member.path for member in state.members] == ["first", "second"]


def test_new_cluster_cannot_supply_missing_n_and_fallback_stays_original():
    first = image("first", 99)
    state = tracker(2)
    assert select(state, scan(first)) is None
    future = image("new-cluster", 104)
    assert select(state, scan(first, future), now=104, wall=104) == first
    assert state.position_unavailable
    assert [member.path for member in state.members] == ["first"]


def test_provisional_idle_fallback_rechecks_timely_members_before_pin():
    first, second = image("first", 100), image("second", 104)
    state = tracker(2)
    assert select(state, scan(first)) is None
    assert select(state, scan(first), now=105, wall=105) == first
    assert state.position_unavailable
    assert select(state, scan(first, second), now=105, wall=105) == second
    assert not state.position_unavailable


def test_resumed_activity_cancels_provisional_idle_fallback_before_commit():
    first = image("first", 99)
    state = tracker(2)
    assert select(state, scan(first)) is None
    assert select(state, scan(first), now=104, wall=104) == first
    assert state.position_unavailable
    growing = image("first", 104, size=11)
    assert select(state, scan(growing), now=104.1, wall=104.1) is None
    assert not state.position_unavailable


def test_confirmed_next_cluster_remains_excluded_after_original_partial_resumes():
    first, second = image("first", 0, size=1), image("second", 1, size=1)
    future = image("future", 6)
    state = tracker(3, freshness=100, lookback=200)
    assert select(state, scan(first, second), now=1, wall=1) is None
    assert select(state, scan(first, second, future), now=6, wall=6) is None
    grown = image("first", 6.2)
    assert select(state, scan(grown, second, future), now=6.2, wall=6.2) == grown
    assert [member.path for member in state.members] == ["first", "second"]
    assert state.position_unavailable
    # Even backdated metadata cannot make the excluded identity an ordinal.
    backdated = image("future", 4)
    assert select(state, scan(grown, second, backdated), now=6.3, wall=6.3) == grown


def test_same_scan_original_write_cannot_bridge_exact_gap_to_future_cluster():
    first = image("first", 99, size=1)
    state = tracker(2)
    assert select(state, scan(first)) is None
    grown, future = image("first", 104.2), image("future", 104)
    assert select(state, scan(grown, future), now=104.2, wall=104.2) == grown
    assert state.position_unavailable
    assert [member.path for member in state.members] == ["first"]
    assert state.next_burst_start_ns == seconds_ns(104)


def test_late_member_before_confirmed_boundary_can_still_supply_n():
    first, second = image("first", 0, size=1), image("second", 1, size=1)
    future, timely = image("future", 6), image("timely", 4)
    state = tracker(3, freshness=100, lookback=200)
    assert select(state, scan(first, second), now=1, wall=1) is None
    assert select(state, scan(first, second, future), now=6, wall=6) is None
    assert (
        select(state, scan(first, second, timely, future), now=6.1, wall=6.1) == timely
    )
    assert not state.position_unavailable


def test_idle_uses_monotonic_time_after_one_historical_age_seed():
    first = image("first", 98)
    state = tracker(2)
    assert select(state, scan(first)) is None
    assert select(state, scan(first), now=102.999, wall=1000000) is None
    assert select(state, scan(first), now=103, wall=0) == first
    assert state.position_unavailable


def test_active_original_writes_reset_idle_without_reordering():
    first = image("first", 98)
    state = tracker(2)
    assert select(state, scan(first)) is None
    written = image("first", 98, size=11)
    assert select(state, scan(written), now=102, wall=110) is None
    assert select(state, scan(written), now=106.999, wall=200) is None
    assert select(state, scan(written), now=107, wall=300) == written


def test_timeout_fallback_skips_small_last_member_within_same_cluster():
    previous, first, small = (
        image("previous", 70),
        image("first", 98),
        image("small", 99, size=1),
    )
    state = tracker(3)
    assert select(state, scan(previous, first, small)) is None
    assert select(state, scan(previous, first, small), now=100.1, timeout=True) == first
    assert state.position_unavailable


@pytest.mark.parametrize("limited", [False, True])
def test_unknown_boundary_never_fabricates_first(limited):
    history = [image(str(stamp), stamp) for stamp in range(38, 99, 4)]
    state = tracker()
    assert select(state, scan(*history, limited=limited)) is None
    assert state.boundary_unknown


def test_prelookback_neighbor_proves_boundary_at_exact_cutoff():
    matches = [image("older", 35)] + [
        image(str(stamp), stamp) for stamp in range(40, 99, 4)
    ]
    state = tracker()
    assert select(state, scan(*matches)).path == "40"
    assert not state.boundary_unknown


def test_earliest_retained_start_is_allowed_and_ties_use_path():
    a, b = image("a", 99), image("b", 99)
    assert select(tracker(), scan(b, a)) == a
    assert select(tracker(2), scan(b, a)) == b


@pytest.mark.parametrize(
    "value,expected",
    [(1, 1), (1.0, 1), (9.0, 9), ("2", 2), (" +12 ", 12), (1000, 1000)],
)
def test_schema_accepts_unbounded_whole_positions(value, expected):
    data = {
        "directory": "/tmp",
        "filename": "",
        "extension": ["WEBP"],
        "fresh_extensions": [".webp"],
        "burst_position": value,
    }
    assert integration.SERVICE_FETCH_SCHEMA(data)["burst_position"] == expected


@pytest.mark.parametrize(
    "value", [True, False, 0, -1, 1.5, "1.5", None, "nan", float("inf"), float("nan")]
)
def test_schema_rejects_invalid_positions(value):
    with pytest.raises(vol.Invalid, match="whole integer"):
        integration.SERVICE_FETCH_SCHEMA(
            {
                "directory": "/tmp",
                "filename": "",
                "extension": ["webp"],
                "fresh_extensions": ["webp"],
                "burst_position": value,
            }
        )


def test_schema_mode_requires_fresh_extensions_and_duration_defaults():
    base = {"directory": "/tmp", "filename": ""}
    data = integration.SERVICE_FETCH_SCHEMA(base)
    assert "burst_position" not in data
    assert data["burst_gap"] == 5
    assert data["lookback_window"] == 60
    with pytest.raises(vol.Invalid, match="requires nonempty fresh_extensions"):
        integration.SERVICE_FETCH_SCHEMA({**base, "burst_position": 1})
    for field in ["burst_gap", "lookback_window"]:
        for value in [0, -1, float("inf"), float("nan"), "invalid"]:
            with pytest.raises(vol.Invalid):
                integration.SERVICE_FETCH_SCHEMA({**base, field: value})


async def fetch(
    hass,
    snapshots,
    *,
    position=1,
    quiet=0.005,
    timeout=0.04,
    gap=5,
    wait=True,
    **kwargs,
):
    count = 0

    def next_scan(*_):
        nonlocal count
        snapshot = snapshots[min(count, len(snapshots) - 1)]
        count += 1
        return snapshot

    with (
        patch.object(upload_wait._Watcher, "start"),
        patch.object(upload_wait._Watcher, "sync"),
        patch.object(upload_wait._Watcher, "stop"),
        patch.object(upload_wait, "scan_files", next_scan),
        patch.object(upload_wait, "refresh_files", next_scan),
    ):
        return await upload_wait.async_fetch_files(
            hass,
            SearchCriteria("/unused", "", {"webp", "mov"}, 5, [], 10, 100),
            wait_for_upload=wait,
            burst_position=position,
            fresh_extensions={"webp"},
            quiet_period=quiet,
            timeout=timeout,
            burst_gap=gap,
            **kwargs,
        )


@pytest.mark.asyncio
async def test_actual_wait_selects_first_image_and_independent_existing_video(hass):
    now = time.time()
    first, last = image("first", now - 3), image("last", now - 1)
    video = image("video", now - 100, extension="mov")
    files, status = await fetch(hass, [scan(first, last, video)])
    assert {match.path for match in files} == {"first", "video"}
    assert status["status"] == "ok"


@pytest.mark.asyncio
async def test_actual_timeout_fallback_retains_observed_readiness(hass):
    now = time.time()
    first, small = image("first", now - 1), image("small", now, size=1)
    files, status = await fetch(hass, [scan(first, small)], position=3)
    assert files == [first]
    assert status["status"] == "fallback"
    assert status["reasons"]["webp"] == ["burst_position_unavailable", "upload_timeout"]
    assert "completion_unverified" not in status["reasons"]["webp"]


@pytest.mark.asyncio
async def test_actual_idle_fallback_requires_ready_same_cluster_member(hass):
    now = time.time()
    first = image("first", now - 0.015)
    files, status = await fetch(hass, [scan(first)], position=2, gap=0.01)
    assert files == [first]
    assert status["reasons"]["webp"] == ["burst_position_unavailable"]


@pytest.mark.asyncio
async def test_actual_final_full_scan_prefers_newly_discovered_n_over_fallback(hass):
    now = time.time()
    first, second = image("first", now - 0.03), image("second", now - 0.02)
    files, status = await fetch(
        hass,
        [scan(first), scan(first, second)],
        position=2,
        gap=0.02,
    )
    assert files == [second]
    assert "burst_position_unavailable" not in status["reasons"].get("webp", [])


@pytest.mark.asyncio
async def test_actual_committed_fallback_never_replaced_by_later_discovery(hass):
    now = time.time()
    first, second = image("first", now - 0.02), image("second", now - 0.015)
    files, status = await fetch(
        hass,
        [scan(first), scan(first), scan(first, second)],
        position=2,
        gap=0.01,
    )
    assert files == [first]
    assert status["reasons"]["webp"] == ["burst_position_unavailable"]


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_actual_loss_after_nth_pin_never_falls_back(hass, replacement):
    now = time.time()
    first, second, third = (
        image("first", now - 2),
        image("second", now - 1),
        image("third", now),
    )
    subsequent = [first, third]
    if replacement:
        subsequent.append(image("second", now, inode=2))
    files, status = await fetch(
        hass, [scan(first, second), scan(*subsequent)], position=2
    )
    assert files == []
    assert "burst_position_unavailable" not in status["reasons"]["webp"]


@pytest.mark.asyncio
async def test_actual_boundary_unknown_omits_image_but_keeps_video(hass):
    now = time.time()
    history = [image(str(i), now - i) for i in range(0, 65, 4)]
    video = image("video", now - 100, extension="mov")
    files, status = await fetch(hass, [scan(*history, video)])
    assert files == [video]
    assert "burst_boundary_unknown" in status["reasons"]["webp"]
    assert status["status"] == "fallback"


@pytest.mark.asyncio
async def test_actual_same_scan_original_growth_does_not_admit_future_cluster(hass):
    now = time.time()
    first = image("first", now - 1, size=1)
    grown = image("first", now + 4.2)
    future = MatchedFile(
        now + 4,
        "future",
        "webp",
        10,
        first.mtime_ns + seconds_ns(5),
        1,
        2,
    )
    files, status = await fetch(
        hass,
        [scan(first), scan(grown, future)],
        position=2,
    )
    assert files == [grown]
    assert "burst_position_unavailable" in status["reasons"]["webp"]


@pytest.mark.asyncio
async def test_no_wait_ignores_burst_mode_and_keeps_latest_qualified(hass):
    now = time.time()
    old, latest, small = (
        image("old", now - 100),
        image("latest", now - 50),
        image("small", now, size=1),
    )
    files, _ = await fetch(hass, [scan(old, latest, small)], wait=False, position=999)
    assert files == [latest]


@pytest.mark.asyncio
async def test_real_second_partial_upload_is_pinned_until_writer_close(hass, tmp_path):
    first = tmp_path / "upload-first.webp"
    first.write_bytes(b"first complete")
    started = threading.Event()
    original_start = upload_wait._Watcher.start

    def start(watcher, criteria):
        original_start(watcher, criteria)
        started.set()

    with patch.object(upload_wait._Watcher, "start", start):
        task = asyncio.create_task(
            upload_wait.async_fetch_files(
                hass,
                SearchCriteria(str(tmp_path), "upload", {"webp"}, 5, [], 10, 100),
                wait_for_upload=True,
                fresh_extensions={"webp"},
                burst_position=2,
                burst_gap=1,
                quiet_period=10,
                timeout=2,
            )
        )
        handle = None
        try:
            assert await hass.async_add_executor_job(started.wait, 1)
            second = tmp_path / "upload-second.webp"
            handle = second.open("wb")
            handle.write(b"x")
            handle.flush()
            await asyncio.sleep(0.03)
            assert not task.done()
            handle.write(b"second finished")
            handle.close()
            files, status = await asyncio.wait_for(task, 1)
            assert [match.path for match in files] == [str(second)]
            assert status["status"] == "ok"
        finally:
            if handle is not None and not handle.closed:
                handle.close()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
