"""Tests for compatibility matrix R/W."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from image_compile.matrix import (
    MatrixEntry, iter_entries, load_matrix, self_blessed_entry, upsert_entry,
    write_matrix,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# self_blessed_entry
# ---------------------------------------------------------------------------

def test_self_blessed_entry_shape() -> None:
    entry = self_blessed_entry("openclaw", "2026.5.5-r1", tested_at=_now())
    assert entry.flavour == "openclaw"
    assert entry.image == "2026.5.5-r1"
    assert entry.template == "image_defaults:openclaw:2026.5.5-r1"
    assert entry.status == "blessed"
    assert entry.test_agent == ""
    assert "Auto-blessed" in entry.notes


def test_self_blessed_entry_notes_overridable() -> None:
    entry = self_blessed_entry("openclaw", "2026.5.5-r1", tested_at=_now(), notes="custom")
    assert entry.notes == "custom"


# ---------------------------------------------------------------------------
# load_matrix
# ---------------------------------------------------------------------------

def test_load_matrix_creates_empty_when_missing(tmp_path: Path) -> None:
    m = load_matrix(tmp_path / "nope.yml")
    assert list(m["entries"]) == []


def test_load_matrix_creates_empty_when_file_blank(tmp_path: Path) -> None:
    p = tmp_path / "matrix.yml"
    p.write_text("", encoding="utf-8")
    m = load_matrix(p)
    assert list(m["entries"]) == []


def test_load_matrix_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    p = tmp_path / "matrix.yml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_matrix(p)


def test_load_matrix_rejects_non_list_entries(tmp_path: Path) -> None:
    p = tmp_path / "matrix.yml"
    p.write_text("entries: foo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list"):
        load_matrix(p)


# ---------------------------------------------------------------------------
# upsert_entry + write_matrix
# ---------------------------------------------------------------------------

def test_upsert_appends_new_entry(tmp_path: Path) -> None:
    p = tmp_path / "matrix.yml"
    m = load_matrix(p)
    entry = self_blessed_entry("openclaw", "2026.5.5-r1", tested_at=_now())
    replaced = upsert_entry(m, entry)
    assert replaced is False
    assert len(m["entries"]) == 1
    write_matrix(p, m)

    reloaded = load_matrix(p)
    assert len(reloaded["entries"]) == 1
    out = list(iter_entries(reloaded))[0]
    assert out["flavour"] == "openclaw"
    assert out["image"] == "2026.5.5-r1"
    assert out["template"] == "image_defaults:openclaw:2026.5.5-r1"
    assert out["status"] == "blessed"


def test_upsert_replaces_existing_by_key(tmp_path: Path) -> None:
    p = tmp_path / "matrix.yml"
    m = load_matrix(p)

    upsert_entry(m, self_blessed_entry("openclaw", "2026.5.5-r1", tested_at=_now()))
    # Simulate operator marking it as experimental — same key, different status.
    second = MatrixEntry(
        flavour="openclaw",
        image="2026.5.5-r1",
        template="image_defaults:openclaw:2026.5.5-r1",
        status="experimental",
        tested_at=_now(),
        test_agent="",
        notes="re-tested by hand",
    )
    replaced = upsert_entry(m, second)
    assert replaced is True
    assert len(m["entries"]) == 1

    write_matrix(p, m)
    out = list(iter_entries(load_matrix(p)))[0]
    assert out["status"] == "experimental"
    assert out["notes"] == "re-tested by hand"


def test_upsert_does_not_collapse_different_keys(tmp_path: Path) -> None:
    p = tmp_path / "matrix.yml"
    m = load_matrix(p)
    upsert_entry(m, self_blessed_entry("openclaw", "2026.5.5-r1", tested_at=_now()))
    upsert_entry(m, self_blessed_entry("openclaw", "2026.5.8-r1", tested_at=_now()))
    upsert_entry(m, self_blessed_entry("nanoclaw", "1.2.3-r1", tested_at=_now()))
    assert len(m["entries"]) == 3
    keys = {(e["flavour"], e["image"], e["template"]) for e in iter_entries(m)}
    assert keys == {
        ("openclaw", "2026.5.5-r1", "image_defaults:openclaw:2026.5.5-r1"),
        ("openclaw", "2026.5.8-r1", "image_defaults:openclaw:2026.5.8-r1"),
        ("nanoclaw", "1.2.3-r1", "image_defaults:nanoclaw:1.2.3-r1"),
    }


# ---------------------------------------------------------------------------
# Comment preservation (the round-trip headline)
# ---------------------------------------------------------------------------

def test_round_trip_preserves_top_level_comments(tmp_path: Path) -> None:
    p = tmp_path / "matrix.yml"
    p.write_text(
        "# Compatibility matrix — operator-edited file. Be gentle.\n"
        "entries:\n"
        "  - flavour: openclaw\n"
        "    image: 2026.5.5-r1\n"
        "    template: image_defaults:openclaw:2026.5.5-r1\n"
        "    status: blessed\n"
        "    tested_at: 2026-05-14T10:00:00Z\n"
        "    test_agent: ''\n"
        "    notes: 'First entry'\n",
        encoding="utf-8",
    )
    m = load_matrix(p)
    # Add another entry and round-trip.
    upsert_entry(m, self_blessed_entry("openclaw", "2026.5.8-r1", tested_at=_now()))
    write_matrix(p, m)

    body = p.read_text(encoding="utf-8")
    assert "# Compatibility matrix — operator-edited file. Be gentle." in body


def test_round_trip_in_place_replace_keeps_position(tmp_path: Path) -> None:
    p = tmp_path / "matrix.yml"
    m = load_matrix(p)
    upsert_entry(m, self_blessed_entry("openclaw", "2026.5.5-r1", tested_at=_now()))
    upsert_entry(m, self_blessed_entry("openclaw", "2026.5.8-r1", tested_at=_now()))
    upsert_entry(m, self_blessed_entry("nanoclaw", "1.2.3-r1", tested_at=_now()))
    write_matrix(p, m)

    # Re-bless 2026.5.5 — its position should not change.
    m2 = load_matrix(p)
    re_blessed = MatrixEntry(
        flavour="openclaw", image="2026.5.5-r1",
        template="image_defaults:openclaw:2026.5.5-r1",
        status="blessed", tested_at=_now(), notes="re-bless",
    )
    upsert_entry(m2, re_blessed)
    write_matrix(p, m2)

    images = [e["image"] for e in iter_entries(load_matrix(p))]
    assert images == ["2026.5.5-r1", "2026.5.8-r1", "1.2.3-r1"]


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def test_write_matrix_atomic_no_leftover_tmp(tmp_path: Path) -> None:
    p = tmp_path / "matrix.yml"
    m = load_matrix(p)
    upsert_entry(m, self_blessed_entry("openclaw", "2026.5.5-r1", tested_at=_now()))
    write_matrix(p, m)
    leftovers = [c.name for c in tmp_path.iterdir() if c.name != "matrix.yml"]
    assert leftovers == []
