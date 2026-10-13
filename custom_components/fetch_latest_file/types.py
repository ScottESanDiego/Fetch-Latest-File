"""Typed contracts for diagnostics, dynamic extension results, and responses."""

from datetime import datetime
from typing import Literal

from typing_extensions import TypedDict

type ReasonCode = Literal[
    "minimum_size",
    "upload_timeout",
    "completion_unverified",
    "no_matching_file",
    "scan_limit_reached",
    "notifications_unavailable",
    "burst_boundary_unknown",
    "burst_position_unavailable",
    "invalid_size",
    "invalid_search",
    "os_error",
]


class FetchStatus(TypedDict):
    """Per-result completion and fallback diagnostics."""

    status: Literal["ok", "fallback", "no_match", "error"]
    reasons: dict[str, list[ReasonCode]]


class TargetResult(TypedDict, extra_items=str):
    """Sensor-native metadata plus dynamic extension/path string entries."""

    timestamp: datetime
    FetchStatus: FetchStatus


class ResponseResult(TypedDict, extra_items=str):
    """JSON response metadata plus dynamic extension/path string entries."""

    timestamp: str
    FetchStatus: FetchStatus


class FetchResponse(TypedDict):
    """Optional action response envelope."""

    target_id: str
    result: ResponseResult
