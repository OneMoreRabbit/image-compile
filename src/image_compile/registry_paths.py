"""Filesystem layout helpers for the platform registry.

Everything that knows the on-disk shape of ~/registry/ goes here so the rest
of the tool can read paths via this module instead of hard-coding strings.

Layout (per docs/image-compile-brief-amendments-v0_1.md Amendment 1):

    <root>/
    ├── image_defaults/<flavour>/<image_version>/
    │   ├── openclaw.json
    │   ├── workspace/
    │   ├── probe-report.yml
    │   └── metadata.yml
    └── compatibility_matrix.yml
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class BundleLayout:
    """Concrete file/directory paths for a single (flavour, image_version) bundle."""
    root: Path
    image_version: str
    flavour: str

    @property
    def bundle_dir(self) -> Path:
        return self.root / "image_defaults" / self.flavour / self.image_version

    @property
    def config_file(self) -> Path:
        # Filename is flavour-agnostic at the registry level; callers know what's inside.
        return self.bundle_dir / "openclaw.json"

    @property
    def workspace_dir(self) -> Path:
        return self.bundle_dir / "workspace"

    @property
    def probe_report(self) -> Path:
        return self.bundle_dir / "probe-report.yml"

    @property
    def metadata_file(self) -> Path:
        return self.bundle_dir / "metadata.yml"

    @property
    def matrix_file(self) -> Path:
        return self.root / "compatibility_matrix.yml"

    def exists(self) -> bool:
        return self.bundle_dir.exists()


def archive_path(archive_root: Path, flavour: str, image_version: str,
                 *, now: datetime | None = None) -> Path:
    """Return the path under archive_root where an existing bundle is moved on --force."""
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    return archive_root / "image_defaults" / flavour / f"{image_version}-{ts}"


def archive_existing_bundle(layout: BundleLayout, archive_root: Path,
                            *, now: datetime | None = None) -> Path | None:
    """If the bundle exists, move it to the archive root. Return the archive path,
    or None if there was nothing to archive."""
    if not layout.exists():
        return None
    target = archive_path(archive_root, layout.flavour, layout.image_version, now=now)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(layout.bundle_dir), str(target))
    return target


def ensure_bundle_dir(layout: BundleLayout) -> None:
    """Create the bundle's directory tree (idempotent)."""
    layout.bundle_dir.mkdir(parents=True, exist_ok=True)
    layout.workspace_dir.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Image-version derivation
# ---------------------------------------------------------------------------

def compute_image_version(upstream_version: str, wrapper_rev: str,
                          *, strip_prefix: str = "") -> str:
    """Combine the upstream tag and wrapper revision into the canonical image_version
    string used in tag names and bundle directory names.

    Example: compute_image_version("v2026.5.5", "r1", strip_prefix="v") == "2026.5.5-r1"
    """
    v = upstream_version
    if strip_prefix and v.startswith(strip_prefix):
        v = v[len(strip_prefix):]
    rev = wrapper_rev if wrapper_rev.startswith("r") else f"r{wrapper_rev}"
    return f"{v}-{rev}"
