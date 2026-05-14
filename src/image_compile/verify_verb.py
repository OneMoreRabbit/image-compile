"""`image-compile verify` verb.

Cross-checks one specific (flavour, image_version) and exits non-zero on any
inconsistency. Intended for scripts and CI gates; a green exit code is the
durable promise that the bundle + matrix + image are aligned.

Checks:
  - Bundle dir exists on disk with all expected files
  - Matrix has an entry for this (flavour, image, template) at status
    `blessed` or `experimental` (`broken` and `missing` both fail)
  - GHCR has the tag (best-effort: failure to reach GHCR doesn't fail verify
    unless `--require-ghcr` is set)
"""
from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console

from . import exit_codes
from .config import Config
from .docker_cli import DockerCLI
from .inventory import build_image_record, load_matrix_for_root


@dataclass
class VerifyOptions:
    flavour: str
    image_version: str
    require_ghcr: bool = False                # if True, GHCR absence is a failure
    json_output: bool = False


def run_verify_verb(cfg: Config, opts: VerifyOptions, *,
                    docker: DockerCLI | None = None,
                    console: Console | None = None,
                    err_console: Console | None = None) -> int:
    console = console or Console()
    err_console = err_console or Console(stderr=True)
    docker = docker or DockerCLI()

    flavour = cfg.require_flavour(opts.flavour)
    matrix = load_matrix_for_root(cfg)

    rec = build_image_record(
        cfg, flavour, opts.image_version,
        matrix=matrix, docker=docker, check_ghcr=opts.require_ghcr,
    )

    if opts.json_output:
        import json
        payload = {
            "flavour": rec.flavour,
            "image_version": rec.image_version,
            "image_tag": rec.image_tag,
            "bundle": rec.bundle.check,
            "bundle_missing_files": list(rec.bundle.missing_files),
            "matrix": rec.matrix.check,
            "matrix_tested_at": rec.matrix.tested_at,
            "ghcr": rec.ghcr.check,
            "consistent": rec.consistent,
            "issues": rec.issues,
        }
        console.print(json.dumps(payload, indent=2))
    else:
        console.print(f"[bold]{rec.flavour}:{rec.image_version}[/]")
        console.print(f"  bundle: {rec.bundle.check}"
                      + (f"  (missing: {', '.join(rec.bundle.missing_files)})"
                         if rec.bundle.missing_files else ""))
        console.print(f"  matrix: {rec.matrix.check}"
                      + (f"  ({rec.matrix.tested_at})"
                         if rec.matrix.tested_at else ""))
        console.print(f"  ghcr:   {rec.ghcr.check}"
                      + (f"  — {rec.ghcr.detail}" if rec.ghcr.detail else ""))

    # Decide exit code
    if rec.bundle.check != "ok":
        err_console.print(f"[red]verify failed:[/red] bundle {rec.bundle.check}")
        return exit_codes.PREFLIGHT_FAILED
    if rec.matrix.check in ("missing", "broken"):
        err_console.print(f"[red]verify failed:[/red] matrix {rec.matrix.check}")
        return exit_codes.PREFLIGHT_FAILED
    if opts.require_ghcr and rec.ghcr.check != "present":
        err_console.print(f"[red]verify failed:[/red] ghcr {rec.ghcr.check}")
        return exit_codes.PREFLIGHT_FAILED

    return exit_codes.OK
