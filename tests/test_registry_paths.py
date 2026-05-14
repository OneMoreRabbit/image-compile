"""Tests for the registry path helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from image_compile.registry_paths import (
    BundleLayout, archive_existing_bundle, archive_path, compute_image_version,
    ensure_bundle_dir,
)


# ---------------------------------------------------------------------------
# compute_image_version
# ---------------------------------------------------------------------------

def test_compute_image_version_strips_prefix() -> None:
    assert compute_image_version("v2026.5.5", "r1", strip_prefix="v") == "2026.5.5-r1"


def test_compute_image_version_handles_missing_r() -> None:
    assert compute_image_version("2026.5.5", "1", strip_prefix="") == "2026.5.5-r1"


def test_compute_image_version_preserves_r_prefix() -> None:
    assert compute_image_version("1.2.3", "r4", strip_prefix="") == "1.2.3-r4"


# ---------------------------------------------------------------------------
# BundleLayout
# ---------------------------------------------------------------------------

def test_bundle_layout_paths(tmp_path: Path) -> None:
    layout = BundleLayout(root=tmp_path, image_version="2026.5.5-r1", flavour="openclaw")
    assert layout.bundle_dir == tmp_path / "image_defaults" / "openclaw" / "2026.5.5-r1"
    assert layout.config_file == layout.bundle_dir / "openclaw.json"
    assert layout.workspace_dir == layout.bundle_dir / "workspace"
    assert layout.probe_report == layout.bundle_dir / "probe-report.yml"
    assert layout.metadata_file == layout.bundle_dir / "metadata.yml"
    assert layout.matrix_file == tmp_path / "compatibility_matrix.yml"
    assert not layout.exists()


def test_bundle_layout_config_file_uses_flavour(tmp_path: Path) -> None:
    layout = BundleLayout(root=tmp_path, image_version="1.2.3-r1", flavour="nanoclaw")
    assert layout.config_file == layout.bundle_dir / "nanoclaw.json"


def test_ensure_bundle_dir_creates_workspace(tmp_path: Path) -> None:
    layout = BundleLayout(root=tmp_path, image_version="x-r1", flavour="openclaw")
    ensure_bundle_dir(layout)
    assert layout.bundle_dir.is_dir()
    assert layout.workspace_dir.is_dir()


# ---------------------------------------------------------------------------
# archive logic
# ---------------------------------------------------------------------------

def test_archive_path_deterministic() -> None:
    now = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    p = archive_path(Path("/tmp/arc"), "openclaw", "1.2.3-r1", now=now)
    assert p == Path("/tmp/arc/image_defaults/openclaw/1.2.3-r1-20260514T100000Z")


def test_archive_existing_bundle_moves_dir(tmp_path: Path) -> None:
    layout = BundleLayout(root=tmp_path / "reg", image_version="2026.5.5-r1", flavour="openclaw")
    ensure_bundle_dir(layout)
    (layout.bundle_dir / "marker.txt").write_text("hello", encoding="utf-8")

    archive_root = tmp_path / "arc"
    moved = archive_existing_bundle(layout, archive_root,
                                    now=datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc))
    assert moved is not None
    assert not layout.bundle_dir.exists()
    assert (moved / "marker.txt").read_text(encoding="utf-8") == "hello"
    assert moved == archive_root / "image_defaults" / "openclaw" / "2026.5.5-r1-20260514T100000Z"


def test_archive_existing_bundle_noop_when_absent(tmp_path: Path) -> None:
    layout = BundleLayout(root=tmp_path, image_version="x-r1", flavour="openclaw")
    moved = archive_existing_bundle(layout, tmp_path / "arc")
    assert moved is None
