"""Parsing and classification of `docker diff` output, and the probe-report
structure that lands in the defaults bundle.

`docker diff <container>` emits lines of the form:

    A /path           added
    C /path           changed (file content)
    D /path           deleted

We split paths into "writes that landed on a surface mount" (already
persistent) and "writes that landed inside the container's ephemeral
filesystem" (candidates for future wrapper relocation, classified per
Amendment 5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Iterator, Literal


ChangeKind = Literal["added", "changed", "deleted"]
RelocationCandidate = Literal["unknown", "unlikely", "likely"]

_DIFF_LINE = re.compile(r"^([ACD])\s+(.+)$")
_KIND_BY_CODE: dict[str, ChangeKind] = {"A": "added", "C": "changed", "D": "deleted"}


# ---------------------------------------------------------------------------
# Raw write records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiffEntry:
    """A single line from `docker diff`."""
    path: str
    change: ChangeKind


def parse_docker_diff(output: str) -> list[DiffEntry]:
    """Parse the raw text output of `docker diff`. Skips blank lines and any
    line that doesn't match the A/C/D prefix format (defensive against
    docker version drift)."""
    entries: list[DiffEntry] = []
    for line in output.splitlines():
        m = _DIFF_LINE.match(line)
        if not m:
            continue
        code, path = m.group(1), m.group(2)
        kind = _KIND_BY_CODE.get(code)
        if kind is None:
            continue
        entries.append(DiffEntry(path=path, change=kind))
    return entries


# ---------------------------------------------------------------------------
# Classification (Amendment 5)
# ---------------------------------------------------------------------------

# Path-pattern rules. Patterns are checked top-to-bottom; the first match wins.
# Anchored to "/" prefix.
_TRANSIENT_PATTERNS = [
    re.compile(r"^/(tmp|run|var/run|proc|sys|dev)(/|$)"),
    re.compile(r".+\.(sock|pid|lock)$"),
]

_LIKELY_PERSISTENT_PATTERNS = [
    re.compile(r".+\.cache/"),
    re.compile(r"^/var/log(/|$)"),
    re.compile(r"^/var/lib(/|$)"),
    re.compile(r".+\.(db|sqlite|sqlite3)$"),
]


def classify_relocation(path: str) -> RelocationCandidate:
    """Apply the Amendment 5 heuristic for a single in-container write path.

    Returns "unknown" when the path doesn't trip any rule. The single-boot
    probe cannot prove a path is frequently rewritten; the architect's
    review of the probe report is the authoritative decision-maker.
    """
    for pat in _TRANSIENT_PATTERNS:
        if pat.search(path):
            return "unlikely"
    for pat in _LIKELY_PERSISTENT_PATTERNS:
        if pat.search(path):
            return "likely"
    return "unknown"


# ---------------------------------------------------------------------------
# Relocation-candidate summary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RelocationSummary:
    """In-container writes flagged as relocation candidates (Amendment 5):
    "likely" paths plus "unknown" paths awaiting the architect's review.
    Only "unlikely" (transient) writes are excluded. Truthy iff any path is
    flagged, so callers can write `if summary:`."""
    likely: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.likely) + len(self.unknown)

    def __bool__(self) -> bool:
        return self.total > 0

    def one_line(self) -> str:
        return (f"{self.total} in-container write(s) flagged as relocation candidates "
                f"({len(self.likely)} likely, {len(self.unknown)} unknown)")


def summarize_relocation_candidates(
        records: Iterable["InContainerWriteRecord"]) -> RelocationSummary:
    likely: list[str] = []
    unknown: list[str] = []
    for r in records:
        if r.candidate_for_relocation == "likely":
            likely.append(r.path)
        elif r.candidate_for_relocation == "unknown":
            unknown.append(r.path)
    return RelocationSummary(likely=tuple(likely), unknown=tuple(unknown))


def summarize_relocation_from_yaml(report: dict) -> RelocationSummary:
    """Same summary, from a parsed probe-report.yml (the bundle's on-disk
    form) — used by `verify`, which has no live ProbeReport object."""
    likely: list[str] = []
    unknown: list[str] = []
    for entry in report.get("in_container_writes") or []:
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("candidate_for_relocation", "unknown")
        path = entry.get("path", "<unknown path>")
        if candidate == "likely":
            likely.append(path)
        elif candidate == "unknown":
            unknown.append(path)
    return RelocationSummary(likely=tuple(likely), unknown=tuple(unknown))


# ---------------------------------------------------------------------------
# Surface partitioning
# ---------------------------------------------------------------------------

DEFAULT_SURFACE_PREFIXES = ("configs", "memory", "sessions", "scratch")


def partition_diff_by_surface(entries: Iterable[DiffEntry], *,
                              mount_root: str = "/agent",
                              surfaces: tuple[str, ...] = DEFAULT_SURFACE_PREFIXES,
                              ) -> tuple[dict[str, list[DiffEntry]], list[DiffEntry]]:
    """Split parsed diff entries into surface_writes and in_container_writes.

    Returns:
        (surface_writes, in_container_writes)
        surface_writes is a dict keyed by surface name (configs/memory/...).
        in_container_writes is the list of paths that fell outside any mount.
    """
    root = mount_root.rstrip("/")
    surface_writes: dict[str, list[DiffEntry]] = {s: [] for s in surfaces}
    in_container: list[DiffEntry] = []
    for e in entries:
        if not e.path.startswith(root + "/"):
            in_container.append(e)
            continue
        # path starts with "/agent/", extract next segment
        rest = e.path[len(root) + 1:]
        first = rest.split("/", 1)[0]
        if first in surface_writes:
            surface_writes[first].append(e)
        else:
            in_container.append(e)
    return surface_writes, in_container


# ---------------------------------------------------------------------------
# Probe report structure
# ---------------------------------------------------------------------------

@dataclass
class HealthCheck:
    status: int | None
    response_time_ms: int | None = None
    body: str | None = None


@dataclass
class SurfaceWriteRecord:
    path: str                                # surface-relative path, e.g. "configs/main/openclaw.json"
    change: ChangeKind
    size_bytes: int | None = None
    kind: str | None = None                  # "directory" if applicable


@dataclass
class InContainerWriteRecord:
    path: str
    change: ChangeKind
    size_bytes: int | None = None
    candidate_for_relocation: RelocationCandidate = "unknown"


ProbeResult = Literal["success", "failed_healthz", "failed_readyz", "crashed", "incomplete"]


@dataclass
class ProbeReport:
    image_tag: str
    flavour: str
    image_version: str
    ran_at: datetime
    duration_seconds: float
    result: ProbeResult
    healthz: HealthCheck | None = None
    readyz: HealthCheck | None = None
    surface_writes: dict[str, list[SurfaceWriteRecord]] = field(default_factory=dict)
    in_container_writes: list[InContainerWriteRecord] = field(default_factory=list)
    observed_paths: dict[str, str | None] = field(default_factory=dict)
    flags_observed: list[str] = field(default_factory=list)
    notes: str = ""

    def to_yaml_dict(self) -> dict:
        """Render the report as a YAML-friendly dict (no datetimes left untouched)."""
        def _ms_or_none(hc: HealthCheck | None) -> dict | None:
            if hc is None:
                return None
            d: dict = {"status": hc.status}
            if hc.response_time_ms is not None:
                d["response_time_ms"] = hc.response_time_ms
            if hc.body is not None:
                d["body"] = hc.body
            return d

        return {
            "probe_metadata": {
                "ran_at": self.ran_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "image_tag": self.image_tag,
                "flavour": self.flavour,
                "image_version": self.image_version,
                "duration_seconds": round(self.duration_seconds, 2),
                "result": self.result,
                "healthz": _ms_or_none(self.healthz),
                "readyz": _ms_or_none(self.readyz),
            },
            "surface_writes": {
                surface: [
                    {k: v for k, v in {
                        "path": r.path, "change": r.change,
                        "size_bytes": r.size_bytes, "kind": r.kind,
                    }.items() if v is not None}
                    for r in records
                ]
                for surface, records in self.surface_writes.items()
            },
            "in_container_writes": [
                {k: v for k, v in {
                    "path": r.path, "change": r.change,
                    "size_bytes": r.size_bytes,
                    "candidate_for_relocation": r.candidate_for_relocation,
                }.items() if v is not None}
                for r in self.in_container_writes
            ],
            "observed_paths": dict(self.observed_paths),
            "flags_observed": list(self.flags_observed),
            "notes": self.notes,
        }


def iter_diff(output: str) -> Iterator[DiffEntry]:
    """Generator counterpart to parse_docker_diff for streaming use."""
    for line in output.splitlines():
        m = _DIFF_LINE.match(line)
        if not m:
            continue
        kind = _KIND_BY_CODE.get(m.group(1))
        if kind is None:
            continue
        yield DiffEntry(path=m.group(2), change=kind)
