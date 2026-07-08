"""Wrapper around `docker buildx build` for the wrapper image.

Builds locally with --load so the image is present in the local Docker daemon
for the smoke test and probe to run against. The push to GHCR is a separate
step (and happens only after probe + bundle succeed).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class BuildPlan:
    image_tag: str                          # ghcr.io/<org>/<line>:<image_version>
    image_version: str                      # 2026.5.5-r1
    upstream_version_no_prefix: str         # 2026.5.5
    wrapper_repo: Path                      # absolute path to the wrapper repo
    wrapper_head_sha: str | None
    upstream_version: str                   # v2026.5.5 (label-only, retains prefix)
    extra_labels: dict[str, str] | None = None
    baked_plugins: tuple[str, ...] = ()     # PINNED specs (@openclaw/whatsapp@2026.6.11)


@dataclass(frozen=True)
class BuildResult:
    returncode: int
    stdout: str
    stderr: str
    argv: list[str]

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


def _construct_argv(plan: BuildPlan, *, source: str) -> list[str]:
    labels = {
        "org.opencontainers.image.source": source,
        "org.opencontainers.image.version": plan.image_version,
        "org.arcpower.openclaw.upstream": plan.upstream_version,
        "org.arcpower.openclaw.wrapper-rev": plan.image_version.split("-")[-1],
    }
    if plan.wrapper_head_sha:
        labels["org.opencontainers.image.revision"] = plan.wrapper_head_sha
    if plan.baked_plugins:
        labels["org.arcpower.openclaw.baked-plugins"] = ",".join(plan.baked_plugins)
    if plan.extra_labels:
        labels.update(plan.extra_labels)

    argv: list[str] = [
        "docker", "buildx", "build",
        "--build-arg", f"OPENCLAW_VERSION={plan.upstream_version_no_prefix}",
        "--tag", plan.image_tag,
        "--platform", "linux/amd64",
        "--load",
    ]
    if plan.baked_plugins:
        argv += ["--build-arg", f"BAKED_PLUGINS={' '.join(plan.baked_plugins)}"]
    for k, v in labels.items():
        argv += ["--label", f"{k}={v}"]
    argv.append(str(plan.wrapper_repo))
    return argv


def run_build(plan: BuildPlan, *, source_label: str | None = None,
              progress_callback: Callable[[str], None] | None = None,
              stream: bool = True) -> BuildResult:
    """Execute `docker buildx build` for the wrapper image.

    When `stream=True` (the default) the output is forwarded to the progress
    callback line-by-line; the returned stdout/stderr are also captured for
    diagnostics on failure. When False, output is captured silently.
    """
    source = source_label or f"image-compile (wrapper={plan.wrapper_head_sha or 'unknown'})"
    argv = _construct_argv(plan, source=source)

    if not stream:
        proc = subprocess.run(argv, check=False, capture_output=True, text=True)
        return BuildResult(returncode=proc.returncode, stdout=proc.stdout,
                           stderr=proc.stderr, argv=argv)

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    out_lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        out_lines.append(line)
        if progress_callback:
            progress_callback(line)
    proc.wait()
    return BuildResult(
        returncode=proc.returncode,
        stdout="\n".join(out_lines),
        stderr="",
        argv=argv,
    )


def construct_argv_for_display(plan: BuildPlan, *, source_label: str | None = None) -> Sequence[str]:
    """Return the argv the runner would execute (used by --dry-run and verbose logging)."""
    return _construct_argv(plan, source=source_label or "image-compile")
