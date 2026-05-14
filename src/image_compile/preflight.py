"""Pre-flight validation for `image-compile build`.

Step 1 of the build workflow per the parent brief. Anything that can fail
without side effects should fail here.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import exit_codes
from . import github_release
from .config import Config, FlavourConfig
from .registry_paths import BundleLayout, compute_image_version


class PreflightError(Exception):
    """Raised when pre-flight detects a problem. Carries the exit code to surface."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreflightResult:
    flavour: FlavourConfig
    upstream_version: str
    upstream_version_no_prefix: str
    wrapper_rev: str
    wrapper_repo: Path
    wrapper_head_sha: str | None
    image_version: str                                      # e.g. "2026.5.5-r1"
    image_tag: str                                          # e.g. "ghcr.io/arcpower/openclaw-runtime:2026.5.5-r1"
    bundle_layout: BundleLayout


def _git_head_sha(path: Path) -> str | None:
    """Return the HEAD SHA of a git repo at `path`, or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def run_preflight(cfg: Config, flavour_name: str, upstream_version: str,
                  wrapper_rev: str, *, wrapper_repo_override: Path | None = None,
                  force: bool = False, validate_upstream: bool = True) -> PreflightResult:
    """Run all pre-flight validations and return the resolved plan.

    Raises PreflightError on any check failure; the caller maps that to an
    exit code.
    """
    flavour = cfg.require_flavour(flavour_name)

    # ---- Wrapper repo path and required files
    wrapper_repo = (wrapper_repo_override or flavour.wrapper_repo_default).resolve()
    if not wrapper_repo.is_dir():
        raise PreflightError(
            f"wrapper repo not found at {wrapper_repo}", exit_codes.PREFLIGHT_FAILED,
        )
    missing = [f for f in flavour.required_wrapper_files if not (wrapper_repo / f).exists()]
    if missing:
        raise PreflightError(
            f"wrapper repo {wrapper_repo} is missing required files: {', '.join(missing)}",
            exit_codes.PREFLIGHT_FAILED,
        )

    wrapper_head_sha = _git_head_sha(wrapper_repo)

    # ---- Image version + tag derivation
    image_version = compute_image_version(
        upstream_version, wrapper_rev,
        strip_prefix=flavour.upstream.version_strip_prefix,
    )
    upstream_no_prefix = image_version.rsplit("-", 1)[0]
    image_tag = f"ghcr.io/{cfg.ghcr.org}/{flavour.image_line}:{image_version}"

    # ---- Upstream existence
    if validate_upstream and flavour.upstream.kind == "github_release":
        if not flavour.upstream.repo:
            raise PreflightError(
                f"flavour {flavour_name!r}: github_release configured but no repo set",
                exit_codes.CONFIG_ERROR,
            )
        try:
            lookup = github_release.check_release(flavour.upstream.repo, upstream_version)
        except github_release.UpstreamReleaseError as e:
            raise PreflightError(str(e), exit_codes.UPSTREAM_NOT_FOUND) from e
        if not lookup.exists:
            raise PreflightError(
                f"upstream release {flavour.upstream.repo}@{upstream_version} not found "
                f"on GitHub (404). Use --no-validate-upstream to skip this check.",
                exit_codes.UPSTREAM_NOT_FOUND,
            )

    # ---- Bundle layout + existence check
    bundle_layout = BundleLayout(
        root=cfg.registry.root,
        image_version=image_version,
        flavour=flavour_name,
    )
    if bundle_layout.exists() and not force:
        raise PreflightError(
            f"defaults bundle already exists at {bundle_layout.bundle_dir} "
            f"(re-run with --force to archive and rebuild)",
            exit_codes.TAG_OR_BUNDLE_EXISTS,
        )

    return PreflightResult(
        flavour=flavour,
        upstream_version=upstream_version,
        upstream_version_no_prefix=upstream_no_prefix,
        wrapper_rev=wrapper_rev,
        wrapper_repo=wrapper_repo,
        wrapper_head_sha=wrapper_head_sha,
        image_version=image_version,
        image_tag=image_tag,
        bundle_layout=bundle_layout,
    )
