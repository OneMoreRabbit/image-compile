"""Standalone `image-compile probe` verb.

Runs the probe loop against an existing locally-loaded or GHCR image. The
heavy logic lives in :mod:`probe`; this module wires the CLI flags through
to that and produces probe-report output (file or stdout) without touching
the registry.

Use cases:
  - Diagnose why a previously-built image needs a re-probe (config drift,
    upstream behaviour change).
  - Test probe-stub changes against an image we didn't just build.
  - Reproduce an old build's probe outcome against the same pinned image.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from rich.console import Console

from . import exit_codes
from .build import PACKAGE_TEMPLATES
from .config import Config
from .docker_cli import DockerCLI, DockerError
from .probe import ProbeError, run_probe


@dataclass
class ProbeVerbOptions:
    flavour: str
    image_tag: str
    pull: bool = False
    output_path: Path | None = None
    quiet: bool = False


def _derive_image_version(image_tag: str) -> str:
    """Best-effort: extract the `<image_version>` segment from a tag like
    `ghcr.io/<org>/<line>:<image_version>` or a bare `name:tag`.

    Used only for labelling the probe report — does not affect probe behaviour.
    """
    if ":" in image_tag:
        return image_tag.rsplit(":", 1)[-1]
    return image_tag


def run_probe_verb(cfg: Config, opts: ProbeVerbOptions, *,
                   docker: DockerCLI | None = None,
                   console: Console | None = None,
                   err_console: Console | None = None) -> int:
    """Execute the standalone probe verb. Returns the desired exit code."""
    console = console or Console(quiet=opts.quiet)
    err_console = err_console or Console(stderr=True)
    docker = docker or DockerCLI(verbose_log=lambda msg: console.print(f"[dim]{msg}[/dim]"))

    flavour = cfg.require_flavour(opts.flavour)

    if opts.pull:
        console.print(f"[bold]pulling[/bold] {opts.image_tag}")
        try:
            docker.run(["pull", opts.image_tag])
        except DockerError as e:
            err_console.print(f"[red]docker pull failed:[/red] {e.stderr.strip() or e}")
            return exit_codes.PROBE_FAILED

    if not docker.image_exists_locally(opts.image_tag):
        err_console.print(
            f"[red]image not loaded locally:[/red] {opts.image_tag}\n"
            f"  pass --pull to fetch from GHCR, or `docker pull` it by hand."
        )
        return exit_codes.PREFLIGHT_FAILED

    image_version = _derive_image_version(opts.image_tag)

    console.print(f"[bold]probing[/bold] {opts.image_tag}")
    try:
        outcome = run_probe(
            flavour, opts.image_tag, image_version,
            templates_root=PACKAGE_TEMPLATES,
            probe_dir_root=cfg.probe_dir_root,
            docker=docker,
            progress_callback=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
        )
    except ProbeError as e:
        err_console.print(f"[red]probe failed:[/red] {e}")
        return e.code

    report_yaml = yaml.safe_dump(
        outcome.report.to_yaml_dict(), sort_keys=False, allow_unicode=True,
    )

    if opts.output_path is None:
        sys.stdout.write(report_yaml)
        sys.stdout.flush()
    else:
        opts.output_path.parent.mkdir(parents=True, exist_ok=True)
        opts.output_path.write_text(report_yaml, encoding="utf-8")
        console.print(f"[green]probe report[/green] written to {opts.output_path}")

    return exit_codes.OK if outcome.report.result == "success" else exit_codes.PROBE_FAILED
