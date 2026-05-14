"""Shared helpers for the `list` and `verify` verbs.

Both verbs need the same primitives:

  - Walk `image_defaults/<flavour>/` to discover known image versions.
  - Load the compatibility matrix and look up entries by (flavour, image, template).
  - Check that a bundle directory has the files we expect.
  - Optionally check whether the tagged image exists in GHCR.

Centralising these here avoids drift between the two verbs and keeps each
verb's orchestrator small.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

from ruamel.yaml.comments import CommentedMap

from .config import Config, FlavourConfig
from .docker_cli import DockerCLI, DockerError
from .matrix import load_matrix
from .registry_paths import BundleLayout


# ---------------------------------------------------------------------------
# Bundle inspection
# ---------------------------------------------------------------------------

BundleCheck = Literal["ok", "missing", "incomplete"]


@dataclass(frozen=True)
class BundleStatus:
    """Outcome of inspecting a single bundle directory on disk."""
    layout: BundleLayout
    check: BundleCheck
    missing_files: tuple[str, ...] = ()
    extra_files: tuple[str, ...] = ()


EXPECTED_TOP_LEVEL_FILES = ("openclaw.json", "probe-report.yml", "metadata.yml")
# Note: openclaw.json is replaced by <flavour>.json at the BundleLayout layer.
# We compute the actual expected name from the layout.


def inspect_bundle(layout: BundleLayout) -> BundleStatus:
    """Check whether a bundle directory exists and contains the expected files."""
    if not layout.bundle_dir.is_dir():
        return BundleStatus(layout=layout, check="missing")

    config_name = layout.config_file.name
    expected = {config_name, "probe-report.yml", "metadata.yml"}
    expected_workspace = {"AGENTS.md", "SOUL.md", "TOOLS.md"}

    missing: list[str] = []
    if not layout.config_file.is_file():
        missing.append(config_name)
    if not layout.probe_report.is_file():
        missing.append("probe-report.yml")
    if not layout.metadata_file.is_file():
        missing.append("metadata.yml")

    ws_dir = layout.workspace_dir
    if not ws_dir.is_dir():
        for w in expected_workspace:
            missing.append(f"workspace/{w}")
    else:
        present = {p.name for p in ws_dir.iterdir() if p.is_file()}
        for w in expected_workspace:
            if w not in present:
                missing.append(f"workspace/{w}")

    check: BundleCheck = "ok" if not missing else "incomplete"
    return BundleStatus(layout=layout, check=check, missing_files=tuple(missing))


# ---------------------------------------------------------------------------
# Matrix lookup
# ---------------------------------------------------------------------------

MatrixCheck = Literal["blessed", "experimental", "broken", "missing"]


@dataclass(frozen=True)
class MatrixStatus:
    check: MatrixCheck
    template: str | None = None
    notes: str | None = None
    tested_at: str | None = None


def lookup_self_blessed(matrix: CommentedMap, flavour: str, image_version: str) -> MatrixStatus:
    """Find the self-blessed entry for (flavour, image_version) in the matrix."""
    template = f"image_defaults:{flavour}:{image_version}"
    for entry in matrix.get("entries", []) or []:
        if (
            entry.get("flavour") == flavour
            and entry.get("image") == image_version
            and entry.get("template") == template
        ):
            status = entry.get("status", "missing")
            if status not in ("blessed", "experimental", "broken"):
                # Treat unrecognised statuses as missing for safety.
                return MatrixStatus(check="missing", template=template)
            return MatrixStatus(
                check=status,
                template=template,
                notes=entry.get("notes"),
                tested_at=entry.get("tested_at"),
            )
    return MatrixStatus(check="missing", template=template)


# ---------------------------------------------------------------------------
# GHCR existence (best-effort)
# ---------------------------------------------------------------------------

GhcrCheck = Literal["present", "absent", "unauthorised", "unchecked"]


@dataclass(frozen=True)
class GhcrStatus:
    """Outcome of probing GHCR for a tagged image. `unchecked` means we deliberately
    skipped the check; `unauthorised` means we tried but auth wasn't available."""
    check: GhcrCheck
    detail: str | None = None


def check_ghcr_tag(docker: DockerCLI, image_tag: str) -> GhcrStatus:
    """Best-effort GHCR existence probe via `docker manifest inspect`.

    Returns:
      - `present`     — manifest fetched successfully
      - `absent`      — manifest 404
      - `unauthorised` — auth failed (no `docker login`, expired token, etc.)
      - `unchecked`   — docker is not available or some other diagnostic-only failure
    """
    try:
        result = docker.run(["manifest", "inspect", image_tag], check=False)
    except DockerError as e:
        return GhcrStatus(check="unchecked", detail=str(e)[:200])

    if result.returncode == 0:
        return GhcrStatus(check="present")

    stderr = (result.stderr or "").lower()
    if "unauthorized" in stderr or "denied" in stderr or "no basic auth" in stderr:
        return GhcrStatus(check="unauthorised", detail=result.stderr.strip()[:200])
    if "not found" in stderr or "manifest unknown" in stderr or "404" in stderr:
        return GhcrStatus(check="absent", detail=result.stderr.strip()[:200])
    return GhcrStatus(check="unchecked", detail=(result.stderr or result.stdout).strip()[:200])


# ---------------------------------------------------------------------------
# Joined per-image record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImageRecord:
    flavour: str
    image_version: str
    image_tag: str
    bundle: BundleStatus
    matrix: MatrixStatus
    ghcr: GhcrStatus

    @property
    def consistent(self) -> bool:
        """True iff bundle + matrix + (ghcr if checked) all agree on existence + blessing."""
        if self.bundle.check != "ok":
            return False
        if self.matrix.check not in ("blessed", "experimental"):
            return False
        if self.ghcr.check == "absent":
            return False
        return True

    @property
    def issues(self) -> list[str]:
        """Human-readable list of inconsistencies for diagnostics."""
        out: list[str] = []
        if self.bundle.check == "missing":
            out.append(f"bundle dir missing at {self.bundle.layout.bundle_dir}")
        elif self.bundle.check == "incomplete":
            out.append(f"bundle missing files: {', '.join(self.bundle.missing_files)}")
        if self.matrix.check == "missing":
            out.append("no matrix entry")
        elif self.matrix.check == "broken":
            out.append("matrix entry marked broken")
        if self.ghcr.check == "absent":
            out.append("image absent from GHCR")
        return out


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

def discover_local_image_versions(cfg: Config, flavour_name: str) -> list[str]:
    """Return the sorted list of image_version dirs present under
    `image_defaults/<flavour>/` (an empty list if the flavour has no bundles yet)."""
    flavour_dir = cfg.registry.root / "image_defaults" / flavour_name
    if not flavour_dir.is_dir():
        return []
    return sorted(
        p.name for p in flavour_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def build_image_record(cfg: Config, flavour: FlavourConfig, image_version: str,
                       *, matrix: CommentedMap, docker: DockerCLI | None = None,
                       check_ghcr: bool = True) -> ImageRecord:
    """Compose bundle + matrix + (optionally) GHCR status for one (flavour, image_version)."""
    layout = BundleLayout(
        root=cfg.registry.root, image_version=image_version, flavour=flavour.name,
    )
    image_tag = f"ghcr.io/{cfg.ghcr.org}/{flavour.image_line}:{image_version}"
    bundle = inspect_bundle(layout)
    matrix_status = lookup_self_blessed(matrix, flavour.name, image_version)
    if check_ghcr and docker is not None:
        ghcr_status = check_ghcr_tag(docker, image_tag)
    else:
        ghcr_status = GhcrStatus(check="unchecked", detail="GHCR check skipped")
    return ImageRecord(
        flavour=flavour.name, image_version=image_version, image_tag=image_tag,
        bundle=bundle, matrix=matrix_status, ghcr=ghcr_status,
    )


def all_known_image_versions(cfg: Config, flavour: FlavourConfig,
                             matrix: CommentedMap) -> list[str]:
    """Union of image versions present on disk and those referenced by matrix entries
    for the flavour. Used by `list` so an orphan matrix entry surfaces too."""
    disk = set(discover_local_image_versions(cfg, flavour.name))
    matrix_versions = {
        e.get("image") for e in (matrix.get("entries") or [])
        if e.get("flavour") == flavour.name and isinstance(e.get("image"), str)
    }
    return sorted(disk | matrix_versions)


def load_matrix_for_root(cfg: Config) -> CommentedMap:
    """Convenience: load the matrix that corresponds to the configured registry root."""
    return load_matrix(cfg.registry.root / "compatibility_matrix.yml")
