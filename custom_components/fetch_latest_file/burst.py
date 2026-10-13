"""Per-call image upload clusters with frozen ordinals and identity pinning."""

from __future__ import annotations

from decimal import Decimal

from .scanner import MatchedFile, ScanResult

type FileIdentity = tuple[str, int, int]


def identity(match: MatchedFile) -> FileIdentity:
    """Separate immutable selection identity from mutable upload metadata."""
    return match.path, match.device, match.inode


def seconds_ns(seconds: float) -> int:
    """Convert positive finite seconds without overflowing intermediate floats."""
    numerator, denominator = float(seconds).as_integer_ratio()
    return numerator * 1_000_000_000 // denominator


class BurstTracker:
    """Follow one extension's original cluster, never a later cluster."""

    def __init__(
        self,
        extension: str,
        position: int,
        gap: float,
        lookback_cutoff_ns: int,
        fresh_cutoff_ns: int,
    ) -> None:
        self.extension = extension
        self.position = position
        self.gap = gap
        numerator, denominator = Decimal(str(gap)).as_integer_ratio()
        # A minimum gap must round up, unlike a lookback duration rounded down.
        self.gap_ns = (numerator * 1_000_000_000 + denominator - 1) // denominator
        self.lookback_cutoff_ns = lookback_cutoff_ns
        self.fresh_cutoff_ns = fresh_cutoff_ns
        self.members: list[MatchedFile] = []
        self.signatures: dict[FileIdentity, tuple[int, int, int, int]] = {}
        self.last_arrival_ns = 0
        self.last_activity = 0.0
        self.ended = False
        self.boundary_unknown = False
        self.position_unavailable = False
        self.next_burst_start_ns: int | None = None
        self.excluded: set[FileIdentity] = set()

    def qualifying_members(self, scan: ScanResult) -> list[MatchedFile]:
        """Current size-qualified originals, in frozen ordinal order."""
        current = {identity(match): match for match in scan.files}
        return [
            current[identity(member)]
            for member in self.members
            if identity(member) in current
        ]

    def select(
        self, scan: ScanResult, *, now: float, wall_now_ns: int, timed_out: bool
    ) -> MatchedFile | None:
        """Choose N, or a same-cluster fallback only after closure/deadline."""
        if self.boundary_unknown:
            return None
        self.position_unavailable = False
        if scan.scan_limit_reached:
            self.boundary_unknown = True
            return None
        matches = sorted(
            (
                match
                for match in (*scan.files, *scan.undersized)
                if match.extension == self.extension
            ),
            key=lambda match: (match.mtime_ns, match.path),
        )
        if not self.members:
            if not matches or matches[-1].mtime_ns < self.fresh_cutoff_ns:
                return None
            start = len(matches) - 1
            while start and (
                matches[start].mtime_ns - matches[start - 1].mtime_ns < self.gap_ns
            ):
                start -= 1
            group = matches[start:]
            if group[0].mtime_ns < self.lookback_cutoff_ns:
                self.boundary_unknown = True
                return None
            self.members = list(group)
            self.signatures = {identity(match): match.signature for match in group}
            self.last_arrival_ns = group[-1].mtime_ns
            # Only seed historical age once; subsequent waits use monotonic time.
            age = max(0, (wall_now_ns - self.last_arrival_ns) / 1_000_000_000)
            self.last_activity = now - age
        else:
            originals = {identity(match) for match in self.members}
            original_paths = {match.path for match in self.members}
            for match in matches:
                key = identity(match)
                if key in originals and self.signatures.get(key) != match.signature:
                    self.signatures[key] = match.signature
                    self.last_activity = now
                    self.ended = self.next_burst_start_ns is not None
            # A final full scan may reveal a timely member missed by refresh scans.
            # Closure remains provisional until the caller commits a fallback pin.
            for match in matches:
                key = identity(match)
                if key in originals or match.path in original_paths:
                    continue
                if key in self.excluded:
                    continue
                if (
                    self.next_burst_start_ns is not None
                    and match.mtime_ns >= self.next_burst_start_ns
                ):
                    self.excluded.add(key)
                    continue
                if match.mtime_ns < self.members[0].mtime_ns:
                    continue  # Earlier history is not part of the anchored cluster.
                if match.mtime_ns - self.last_arrival_ns >= self.gap_ns:
                    self.ended = True
                    self.next_burst_start_ns = match.mtime_ns
                    self.excluded.add(key)
                    continue
                self.members.append(match)
                originals.add(key)
                original_paths.add(match.path)
                self.signatures[key] = match.signature
                self.last_arrival_ns = max(self.last_arrival_ns, match.mtime_ns)
                self.last_activity = now
                self.ended = self.next_burst_start_ns is not None
        if len(self.members) >= self.position:
            self.position_unavailable = False
            # Missing/replaced N remains the pin's identity, never a fallback.
            return self.members[self.position - 1]
        if now - self.last_activity >= self.gap:
            self.ended = True
        if self.ended or timed_out:
            self.position_unavailable = True
            available = self.qualifying_members(scan)
            return available[-1] if available else None
        return None
