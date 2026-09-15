"""Tests for docker-diff parsing and write classification."""
from __future__ import annotations

import pytest

from image_compile.probe_report import (
    DiffEntry, InContainerWriteRecord, RelocationSummary, classify_relocation, parse_docker_diff,
    partition_diff_by_surface, summarize_relocation_candidates,
    summarize_relocation_from_yaml,
)


# ---------------------------------------------------------------------------
# parse_docker_diff
# ---------------------------------------------------------------------------

def test_parse_docker_diff_basic() -> None:
    out = """A /agent/configs/main/openclaw.json
C /home/agent/.openclaw
D /tmp/foo"""
    entries = parse_docker_diff(out)
    assert entries == [
        DiffEntry(path="/agent/configs/main/openclaw.json", change="added"),
        DiffEntry(path="/home/agent/.openclaw", change="changed"),
        DiffEntry(path="/tmp/foo", change="deleted"),
    ]


def test_parse_docker_diff_skips_blank_and_unrecognised() -> None:
    out = "\nA /one\ngarbage line\nC /two\n\nE /unknown-code\n"
    entries = parse_docker_diff(out)
    assert [e.path for e in entries] == ["/one", "/two"]


def test_parse_docker_diff_empty() -> None:
    assert parse_docker_diff("") == []


# ---------------------------------------------------------------------------
# classify_relocation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/tmp/openclaw-1234.sock",            "unlikely"),
    ("/run/agent.pid",                     "unlikely"),
    ("/var/run/openclaw.lock",             "unlikely"),
    ("/proc/123/maps",                     "unlikely"),
    ("/home/agent/.cache/openclaw/cache.db", "likely"),
    ("/var/lib/openclaw/state.sqlite",     "likely"),
    ("/var/log/openclaw.log",              "likely"),
    ("/home/agent/.openclaw/exec-approvals.json", "unknown"),
    ("/opt/wrapper/entrypoint.sh",         "unknown"),
])
def test_classify_relocation_heuristic(path: str, expected: str) -> None:
    assert classify_relocation(path) == expected


# ---------------------------------------------------------------------------
# partition_diff_by_surface
# ---------------------------------------------------------------------------

def test_partition_separates_surface_and_container_writes() -> None:
    entries = [
        DiffEntry(path="/agent/configs/main/openclaw.json", change="changed"),
        DiffEntry(path="/agent/memory/main/workspace",     change="added"),
        DiffEntry(path="/agent/sessions/main/log.jsonl",   change="added"),
        DiffEntry(path="/agent/scratch/main",              change="added"),
        DiffEntry(path="/home/agent/.openclaw/lockfile",   change="added"),
        DiffEntry(path="/agent/unknown/main/foo",          change="added"),
    ]
    surface_writes, in_container = partition_diff_by_surface(entries)
    assert [e.path for e in surface_writes["configs"]] == ["/agent/configs/main/openclaw.json"]
    assert [e.path for e in surface_writes["memory"]] == ["/agent/memory/main/workspace"]
    assert [e.path for e in surface_writes["sessions"]] == ["/agent/sessions/main/log.jsonl"]
    assert [e.path for e in surface_writes["scratch"]] == ["/agent/scratch/main"]
    # The /agent/unknown/ path doesn't match any surface, so it lands in in_container.
    assert {e.path for e in in_container} == {
        "/home/agent/.openclaw/lockfile",
        "/agent/unknown/main/foo",
    }


def test_partition_paths_outside_mount_root_are_in_container() -> None:
    entries = [
        DiffEntry(path="/tmp/foo", change="added"),
        DiffEntry(path="/usr/local/bin/openclaw", change="changed"),
    ]
    surface_writes, in_container = partition_diff_by_surface(entries)
    assert all(records == [] for records in surface_writes.values())
    assert {e.path for e in in_container} == {"/tmp/foo", "/usr/local/bin/openclaw"}


# ---------------------------------------------------------------------------
# summarize_relocation_candidates / summarize_relocation_from_yaml
# ---------------------------------------------------------------------------

def test_summarize_relocation_candidates_flags_likely_and_unknown() -> None:
    records = [
        InContainerWriteRecord(path="/home/agent/.openclaw/state/openclaw.sqlite",
                               change="added", candidate_for_relocation="likely"),
        InContainerWriteRecord(path="/home/agent/.openclaw/credentials",
                               change="added", candidate_for_relocation="unknown"),
        InContainerWriteRecord(path="/tmp/openclaw.sock",
                               change="added", candidate_for_relocation="unlikely"),
    ]
    summary = summarize_relocation_candidates(records)
    assert summary.likely == ("/home/agent/.openclaw/state/openclaw.sqlite",)
    assert summary.unknown == ("/home/agent/.openclaw/credentials",)
    assert summary.total == 2
    assert bool(summary) is True
    assert "2 in-container write(s)" in summary.one_line()
    assert "1 likely" in summary.one_line()
    assert "1 unknown" in summary.one_line()


def test_summarize_relocation_candidates_empty_when_all_unlikely() -> None:
    records = [
        InContainerWriteRecord(path="/run/agent.pid", change="added",
                               candidate_for_relocation="unlikely"),
    ]
    summary = summarize_relocation_candidates(records)
    assert summary.total == 0
    assert bool(summary) is False


def test_summarize_relocation_from_yaml_matches_report_shape() -> None:
    # The on-disk shape written by ProbeReport.to_yaml_dict().
    report = {
        "in_container_writes": [
            {"path": "/home/agent/.openclaw/state/openclaw.sqlite",
             "change": "added", "candidate_for_relocation": "likely"},
            {"path": "/home/agent/.npm/_cacache/x", "change": "added",
             "candidate_for_relocation": "unknown"},
            {"path": "/tmp/x.lock", "change": "added",
             "candidate_for_relocation": "unlikely"},
        ],
    }
    summary = summarize_relocation_from_yaml(report)
    assert summary.likely == ("/home/agent/.openclaw/state/openclaw.sqlite",)
    assert summary.unknown == ("/home/agent/.npm/_cacache/x",)


def test_summarize_relocation_from_yaml_tolerates_missing_and_malformed() -> None:
    assert summarize_relocation_from_yaml({}).total == 0
    assert summarize_relocation_from_yaml({"in_container_writes": None}).total == 0
    # A record missing the candidate key defaults to "unknown" (flagged).
    summary = summarize_relocation_from_yaml(
        {"in_container_writes": [{"path": "/x", "change": "added"}, "garbage"]})
    assert summary.unknown == ("/x",)


# ---------------------------------------------------------------------------
# render_paths -- both classes listed, every cap announcing its remainder
# ---------------------------------------------------------------------------

def _summary(n_likely: int, n_unknown: int) -> RelocationSummary:
    return RelocationSummary(
        likely=tuple(f"/likely/{i}" for i in range(n_likely)),
        unknown=tuple(f"/unknown/{i}" for i in range(n_unknown)),
    )


def test_render_paths_lists_unknown_not_just_likely() -> None:
    """The regression this fix exists for: a 0-likely build is the GOOD case and
    used to print a warning with nothing to review, because the loop iterated
    `likely` only. `unknown` is the class the architect must actually see."""
    lines = _summary(0, 3).render_paths()
    assert len(lines) == 3
    assert all("unknown" in ln for ln in lines)
    assert "/unknown/0" in "\n".join(lines)


def test_render_paths_lists_likely_first() -> None:
    lines = _summary(2, 2).render_paths()
    assert "likely" in lines[0] and "likely" in lines[1]
    assert "unknown" in lines[2] and "unknown" in lines[3]


def test_every_cap_announces_its_remainder() -> None:
    """A cap that hides what it dropped is a truthful-looking answer over an
    incomplete set -- the defect this fix was told not to re-create one function
    later. Both classes over the cap, both remainders stated."""
    lines = _summary(14, 25).render_paths(cap=10)
    joined = "\n".join(lines)
    assert "and 4 more" in joined, "likely remainder not announced"
    assert "and 15 more" in joined, "unknown remainder not announced"
    assert joined.count("see probe-report.yml") == 2


def test_printed_lines_reconcile_with_the_headline_count() -> None:
    """Paths shown + remainders announced must equal one_line()'s total, or the
    output contradicts its own summary."""
    s = _summary(14, 25)
    lines = s.render_paths(cap=10)
    shown = sum(1 for ln in lines if "more —" not in ln)
    announced = sum(int(ln.split("and ")[1].split(" more")[0])
                    for ln in lines if "more —" in ln)
    assert shown + announced == s.total == 39


def test_render_paths_empty_when_nothing_flagged() -> None:
    assert _summary(0, 0).render_paths() == []


def test_no_remainder_line_when_exactly_at_the_cap() -> None:
    """Off-by-one guard: 10 paths at cap 10 is complete, not truncated."""
    lines = _summary(0, 10).render_paths(cap=10)
    assert len(lines) == 10
    assert "more" not in "\n".join(lines)
