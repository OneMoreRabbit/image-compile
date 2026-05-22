"""The `build` verb's orchestrator.

Implements the build workflow described in the parent brief, minus the GHCR
push and compatibility-matrix update (those are Phase 2). For Phase 1:

    preflight → docker build → probe → bundle → summary

Atomicity (parent brief §Inputs and outputs):
- The bundle is written to disk first.
- The image push happens last (deferred to Phase 2).
- Failures leave diagnostic artifacts in `.last-failed/` and remove the
  locally-loaded image unless `--keep-on-failure` is passed.
"""
from __future__ import annotations

import getpass
import os
import platform
import shutil
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from . import exit_codes
from .bundle import build_metadata, write_bundle
from .config import Config
from .docker_build import BuildPlan, construct_argv_for_display, run_build
from .docker_cli import DockerCLI, DockerError
from .matrix import load_matrix, self_blessed_entry, upsert_entry, write_matrix
from .preflight import PreflightError, run_preflight
from .probe import ProbeError, run_probe
from .registry_paths import archive_existing_bundle, ensure_bundle_dir


PACKAGE_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "templates"


@dataclass
class BuildOptions:
    flavour: str
    upstream_version: str
    wrapper_rev: str = "r1"
    wrapper_repo: Path | None = None
    no_push: bool = False
    no_bundle: bool = False
    force: bool = False
    validate_upstream: bool = True
    keep_on_failure: bool = False
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Failure-artifact helpers
# ---------------------------------------------------------------------------

def _last_failed_dir(stage: str) -> Path:
    """Where diagnostic artifacts go on failure."""
    base = Path.home() / ".local" / "state" / "image-compile" / "last-failed" / stage
    base.mkdir(parents=True, exist_ok=True)
    return base


def _write_failure_artifact(stage: str, name: str, contents: str) -> Path:
    path = _last_failed_dir(stage) / name
    path.write_text(contents, encoding="utf-8")
    return path


def _remove_local_image(docker: DockerCLI, image_tag: str, console: Console) -> None:
    try:
        docker.rm_image(image_tag, force=True)
        console.print(f"[dim]removed locally-loaded image {image_tag}[/dim]")
    except DockerError:
        # rm_image already passes check=False; ignore.
        pass


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_build_verb(cfg: Config, opts: BuildOptions, *,
                   console: Console | None = None,
                   err_console: Console | None = None,
                   docker: DockerCLI | None = None) -> int:
    """Execute `image-compile build`. Returns the desired exit code."""
    console = console or Console()
    err_console = err_console or Console(stderr=True)
    docker = docker or DockerCLI(verbose_log=lambda msg: console.print(f"[dim]{msg}[/dim]"))

    # ----- Preflight ----------------------------------------------------
    try:
        plan = run_preflight(
            cfg, opts.flavour, opts.upstream_version, opts.wrapper_rev,
            wrapper_repo_override=opts.wrapper_repo,
            force=opts.force,
            validate_upstream=opts.validate_upstream,
        )
    except PreflightError as e:
        err_console.print(f"[red]preflight failed:[/red] {e}")
        return e.code

    console.print(f"[green]preflight ok[/green] — image will be tagged [bold]{plan.image_tag}[/bold]")
    console.print(f"  wrapper repo: {plan.wrapper_repo} (HEAD: {plan.wrapper_head_sha or 'unknown'})")
    console.print(f"  bundle dir:   {plan.bundle_layout.bundle_dir}")

    # ----- Archive existing bundle on --force ---------------------------
    if opts.force and plan.bundle_layout.exists():
        archived = archive_existing_bundle(plan.bundle_layout, cfg.registry.archive_root)
        if archived:
            console.print(f"  archived previous bundle to {archived}")

    if opts.dry_run:
        argv = construct_argv_for_display(BuildPlan(
            image_tag=plan.image_tag,
            image_version=plan.image_version,
            upstream_version_no_prefix=plan.upstream_version_no_prefix,
            wrapper_repo=plan.wrapper_repo,
            wrapper_head_sha=plan.wrapper_head_sha,
            upstream_version=plan.upstream_version,
        ))
        console.print("[yellow]--dry-run[/yellow] — would run:")
        console.print("  " + " ".join(argv))
        return exit_codes.OK

    # ----- Build --------------------------------------------------------
    build_plan = BuildPlan(
        image_tag=plan.image_tag,
        image_version=plan.image_version,
        upstream_version_no_prefix=plan.upstream_version_no_prefix,
        wrapper_repo=plan.wrapper_repo,
        wrapper_head_sha=plan.wrapper_head_sha,
        upstream_version=plan.upstream_version,
    )
    console.print(f"[bold]building[/bold] {plan.image_tag}")
    build_started = time.monotonic()
    build_result = run_build(
        build_plan,
        progress_callback=lambda line: console.print(f"  [dim]{line}[/dim]") if line.strip() else None,
    )
    build_duration = time.monotonic() - build_started
    if not build_result.succeeded:
        _write_failure_artifact("build", "stdout.log", build_result.stdout)
        _write_failure_artifact("build", "stderr.log", build_result.stderr or "")
        err_console.print(f"[red]docker build failed[/red] (rc={build_result.returncode}) "
                          f"after {build_duration:.1f}s — see ~/.local/state/image-compile/last-failed/build/")
        return exit_codes.DOCKER_BUILD_FAILED
    console.print(f"[green]image built[/green] in {build_duration:.1f}s")

    # ----- Probe --------------------------------------------------------
    if opts.no_bundle:
        console.print("[yellow]--no-bundle[/yellow] — skipping probe and bundle assembly")
        return exit_codes.OK

    try:
        probe_outcome = run_probe(
            plan.flavour, plan.image_tag, plan.image_version,
            templates_root=PACKAGE_TEMPLATES,
            probe_dir_root=cfg.probe_dir_root,
            docker=docker,
            progress_callback=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
        )
    except ProbeError as e:
        _write_failure_artifact("probe", "error.txt", str(e))
        err_console.print(f"[red]probe failed:[/red] {e}")
        if not opts.keep_on_failure and cfg.remove_local_image_on_failure:
            _remove_local_image(docker, plan.image_tag, console)
        return e.code

    # An unhealthy gateway means we don't trust the captured config as the
    # basis for an official defaults bundle. Persist diagnostic artifacts and
    # exit; no bundle goes into the registry.
    if probe_outcome.report.result != "success":
        _write_failure_artifact("probe", "container.log", probe_outcome.container_logs)
        _write_failure_artifact("probe", "docker-diff.txt", probe_outcome.diff_output)
        import yaml
        _write_failure_artifact(
            "probe", "probe-report.yml",
            yaml.safe_dump(probe_outcome.report.to_yaml_dict(), sort_keys=False),
        )
        err_console.print(
            f"[red]probe failed:[/red] result={probe_outcome.report.result}, "
            f"healthz={probe_outcome.report.healthz.status if probe_outcome.report.healthz else 'n/a'}. "
            f"Diagnostic artifacts at ~/.local/state/image-compile/last-failed/probe/"
        )
        if not opts.keep_on_failure and cfg.remove_local_image_on_failure:
            _remove_local_image(docker, plan.image_tag, console)
        return exit_codes.PROBE_FAILED

    console.print(f"[green]probe ok[/green] (result={probe_outcome.report.result})")

    # ----- Bundle assembly ---------------------------------------------
    if probe_outcome.captured_openclaw_json is None:
        err_console.print("[red]probe produced no captured openclaw.json[/red] — cannot assemble bundle")
        if not opts.keep_on_failure and cfg.remove_local_image_on_failure:
            _remove_local_image(docker, plan.image_tag, console)
        return exit_codes.PROBE_FAILED

    ensure_bundle_dir(plan.bundle_layout)

    metadata = build_metadata(
        image_tag=plan.image_tag,
        image_version=plan.image_version,
        flavour_name=plan.flavour.name,
        upstream_version=plan.upstream_version,
        wrapper_rev=plan.wrapper_rev,
        wrapper_repo_head=plan.wrapper_head_sha,
        build_date=datetime.now(timezone.utc),
        built_by=getpass.getuser(),
        built_on=socket.gethostname(),
    )

    try:
        written = write_bundle(
            plan.bundle_layout, plan.flavour,
            captured_openclaw_json=probe_outcome.captured_openclaw_json,
            probe_report=probe_outcome.report,
            metadata=metadata,
            templates_root=PACKAGE_TEMPLATES,
        )
    except (OSError, FileNotFoundError) as e:
        err_console.print(f"[red]bundle assembly failed:[/red] {e}")
        if not opts.keep_on_failure and cfg.remove_local_image_on_failure:
            _remove_local_image(docker, plan.image_tag, console)
        return exit_codes.REGISTRY_WRITE_FAILED

    console.print(f"[green]bundle written[/green]: {len(written)} files at {plan.bundle_layout.bundle_dir}")
    for p in written:
        try:
            rel = p.relative_to(plan.bundle_layout.bundle_dir)
        except ValueError:
            rel = p
        console.print(f"  [dim]{rel}[/dim]")

    # ----- Push --------------------------------------------------------
    pushed = False
    if opts.no_push:
        console.print("[dim]--no-push: skipping GHCR push as requested[/dim]")
    else:
        console.print(f"[bold]pushing[/bold] {plan.image_tag}")
        try:
            docker.run(["push", plan.image_tag])
            pushed = True
            console.print("[green]pushed[/green]")
        except DockerError as e:
            _write_failure_artifact("push", "stderr.log", e.stderr or str(e))
            err_console.print(
                f"[red]docker push failed:[/red] {e.stderr.strip() or e}\n"
                f"  the image is built locally and the bundle is on disk.\n"
                f"  once GHCR auth is fixed, re-run `image-compile build {opts.flavour} "
                f"{opts.upstream_version} --force` to complete the push and matrix update."
            )
            return exit_codes.PUSH_FAILED

    # ----- Matrix update ----------------------------------------------
    if pushed or opts.no_push:
        try:
            mtx = load_matrix(plan.bundle_layout.matrix_file)
            entry = self_blessed_entry(
                flavour=plan.flavour.name,
                image_version=plan.image_version,
                tested_at=datetime.now(timezone.utc),
            )
            replaced = upsert_entry(mtx, entry)
            write_matrix(plan.bundle_layout.matrix_file, mtx)
            verb = "updated" if replaced else "appended"
            console.print(f"[green]matrix[/green] {verb} self-blessed entry for "
                          f"{plan.flavour.name}:{plan.image_version}")
        except (OSError, ValueError) as e:
            err_console.print(f"[red]matrix write failed:[/red] {e}")
            return exit_codes.REGISTRY_WRITE_FAILED

    # ----- Summary -----------------------------------------------------
    console.print()
    console.print(f"[bold green]ok[/bold green]: {plan.image_tag}")
    console.print(f"  bundle: {plan.bundle_layout.bundle_dir}")
    if pushed:
        console.print(f"  ghcr:   pushed")
    elif opts.no_push:
        console.print(f"  ghcr:   skipped (--no-push)")
    console.print(f"  matrix: {plan.bundle_layout.matrix_file}")
    return exit_codes.OK
