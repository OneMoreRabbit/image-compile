"""Tests for docker-diff parsing and write classification."""
from __future__ import annotations

import pytest

from image_compile.probe_report import (
    DiffEntry, InContainerWriteRecord, classify_relocation, parse_docker_diff,
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
