"""Pre-flight validation for `image-compile build`.

Step 1 of the build workflow per the parent brief. Anything that can fail
without side effects should fail here.
"""
from __future__ import annotations

import re
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
    image_tag: str                                          # e.g. "ghcr.io/onemorerabbit/openclaw-runtime:2026.5.5-r1"
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


def _git_is_dirty(path: Path) -> bool | None:
    """True/False if the worktree at `path` has uncommitted changes; None if undeterminable.

    None is NOT "clean" -- the caller must treat it as a failed check, not a pass.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


_CHANGELOG_REV_RE = re.compile(r"^##\s+(r\d+(?:\.\d+)*)\b", re.MULTILINE)


def changelog_top_rev(wrapper_repo: Path, filename: str = "CHANGELOG.md") -> str | None:
    """The topmost `## r<N>` heading in the wrapper's CHANGELOG, or None if unreadable.

    This is the wrapper repo's own statement of which revision its tree is at.
    """
    path = wrapper_repo / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _CHANGELOG_REV_RE.search(text)
    return m.group(1) if m else None


def check_wrapper_rev(wrapper_repo: Path, wrapper_rev: str, *,
                      allow_dirty: bool = False,
                      require_changelog: bool = True,
                      expected_commit: str | None = None) -> None:
    """Tie `--wrapper-rev` to the tree actually being built. Raises PreflightError.

    The r3 incident (2026-05-22): `--wrapper-rev r3` was built from a checkout
    frozen at the pre-r3 scaffold commit, producing an image labelled r3 whose
    `revision` was `655d9caa`. Nothing detected it; the mislabelled image reached
    GHCR and cost several deploy cycles of misdirected debugging.

    Both checks fail CLOSED. An undeterminable result is a failure, not a pass --
    that substitution is the failure class this guard exists to break.

    `expected_commit` closes the gap the first two cannot (catalogue 0.44): a
    clean tree whose CHANGELOG says r8.1 is *a* tree claiming that revision, not
    *the* tree anyone recorded. Both checks read properties of whatever tree they
    run in, so they validate a self-asserted identity against itself. Measured
    2026-09-15: `openclaw-runtime` had `main` at 25a8b67 and `dev` at 8bbfe22,
    both clean, both with `## r8.1` topmost -- so both passed, while only one was
    the recorded input. Pass the recorded commit here and the comparison is
    against a value held OUTSIDE the tree being guarded.
    """
    dirty = _git_is_dirty(wrapper_repo)
    if dirty is None:
        raise PreflightError(
            f"cannot determine whether the wrapper repo {wrapper_repo} is clean "
            f"(not a git repo, or git unavailable). The recorded revision would not "
            f"describe what was built. Use --allow-dirty to build anyway.",
            exit_codes.PREFLIGHT_FAILED,
        )
    if dirty and not allow_dirty:
        raise PreflightError(
            f"wrapper repo {wrapper_repo} has uncommitted changes, so the recorded "
            f"revision would not describe the image contents. Commit them, or pass "
            f"--allow-dirty to accept an unreproducible build.",
            exit_codes.PREFLIGHT_FAILED,
        )

    if expected_commit:
        check_wrapper_commit(wrapper_repo, expected_commit)

    if not require_changelog:
        return
    top = changelog_top_rev(wrapper_repo)
    if top is None:
        raise PreflightError(
            f"wrapper repo {wrapper_repo} has no readable `## r<N>` heading in "
            f"CHANGELOG.md, so --wrapper-rev {wrapper_rev} cannot be verified against "
            f"the tree. Add the heading, or set `wrapper_rev_check: false` on this "
            f"flavour to declare that it does not use the r<rev> convention.",
            exit_codes.PREFLIGHT_FAILED,
        )
    if top != wrapper_rev:
        raise PreflightError(
            f"--wrapper-rev {wrapper_rev} does not match the wrapper tree: "
            f"{wrapper_repo}/CHANGELOG.md declares {top} as its latest revision. "
            f"Building r{wrapper_rev.lstrip('r')} from a tree that says {top} is the "
            f"r3 incident. Check out the right commit, or correct --wrapper-rev.",
            exit_codes.PREFLIGHT_FAILED,
        )


def check_wrapper_commit(wrapper_repo: Path, expected_commit: str) -> None:
    """Assert the wrapper checkout is at the commit the caller recorded.

    The only check here that compares against something the guarded tree cannot
    assert about itself. Accepts an abbreviated SHA (git's own rules: a prefix,
    minimum 7 characters) so a recorded short sha works unchanged.
    """
    expected = expected_commit.strip().lower()
    if len(expected) < 7 or any(c not in "0123456789abcdef" for c in expected):
        raise PreflightError(
            f"--wrapper-commit {expected_commit!r} is not a usable commit id "
            f"(hex, at least 7 characters).",
            exit_codes.PREFLIGHT_FAILED,
        )
    head = _git_head_sha(wrapper_repo)
    if head is None:
        raise PreflightError(
            f"cannot determine HEAD of the wrapper repo {wrapper_repo}, so "
            f"--wrapper-commit {expected} cannot be verified.",
            exit_codes.PREFLIGHT_FAILED,
        )
    if not head.lower().startswith(expected):
        raise PreflightError(
            f"wrapper repo {wrapper_repo} is at {head[:12]}, not the recorded "
            f"--wrapper-commit {expected}. A clean tree whose CHANGELOG names the "
            f"right revision is not evidence it is the right tree -- check out "
            f"{expected} (detached is fine), or correct --wrapper-commit.",
            exit_codes.PREFLIGHT_FAILED,
        )


def run_preflight(cfg: Config, flavour_name: str, upstream_version: str,
                  wrapper_rev: str, *, wrapper_repo_override: Path | None = None,
                  force: bool = False, validate_upstream: bool = True,
                  allow_dirty: bool = False,
                  expected_commit: str | None = None) -> PreflightResult:
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

    # ---- Wrapper revision integrity (the r3 incident guard)
    check_wrapper_rev(
        wrapper_repo, wrapper_rev,
        allow_dirty=allow_dirty,
        require_changelog=flavour.wrapper_rev_check,
        expected_commit=expected_commit,
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
