"""`image-compile list` verb.

Enumerates known image versions per flavour and pairs each against the
defaults bundle on disk, the compatibility matrix entry, and (best-effort)
the GHCR tag. Reports inconsistencies but does not modify any state.
"""
from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from . import exit_codes
from .config import Config
from .docker_cli import DockerCLI
from .inventory import (
    GhcrStatus, ImageRecord, all_known_image_versions, build_image_record,
    load_matrix_for_root,
)


@dataclass
class ListOptions:
    flavour: str | None = None                # None = all configured flavours
    check_ghcr: bool = True
    json_output: bool = False


# ---------------------------------------------------------------------------
# Pretty rendering
# ---------------------------------------------------------------------------

_BUNDLE_GLYPH = {"ok": "[green]✓[/]", "incomplete": "[yellow]△[/]", "missing": "[red]✗[/]"}
_MATRIX_GLYPH = {
    "blessed":      "[green]✓ blessed[/]",
    "experimental": "[yellow]· experimental[/]",
    "broken":       "[red]✗ broken[/]",
    "missing":      "[red]✗ missing[/]",
}
_GHCR_GLYPH = {
    "present":      "[green]✓[/]",
    "absent":       "[red]✗[/]",
    "unauthorised": "[yellow]?[/] auth",
    "unchecked":    "[dim]—[/]",
}


def _record_row(rec: ImageRecord) -> tuple[str, str, str, str, str]:
    return (
        rec.image_version,
        _BUNDLE_GLYPH[rec.bundle.check],
        _GHCR_GLYPH[rec.ghcr.check],
        _MATRIX_GLYPH[rec.matrix.check],
        ", ".join(rec.issues) if rec.issues else "",
    )


def _render_tables(console: Console, records_by_flavour: dict[str, list[ImageRecord]]) -> None:
    for flavour, records in records_by_flavour.items():
        if not records:
            console.print(f"[bold]{flavour}[/]: [dim](no images)[/]")
            continue
        table = Table(title=flavour, title_style="bold", show_lines=False, expand=False)
        table.add_column("image_version", style="cyan")
        table.add_column("bundle", justify="center")
        table.add_column("ghcr", justify="center")
        table.add_column("matrix")
        table.add_column("issues", style="dim")
        for rec in records:
            table.add_row(*_record_row(rec))
        console.print(table)


def _render_json(console: Console, records_by_flavour: dict[str, list[ImageRecord]]) -> None:
    import json
    payload = {
        flavour: [
            {
                "image_version": r.image_version,
                "image_tag": r.image_tag,
                "bundle": r.bundle.check,
                "bundle_missing_files": list(r.bundle.missing_files),
                "ghcr": r.ghcr.check,
                "matrix": r.matrix.check,
                "matrix_tested_at": r.matrix.tested_at,
                "consistent": r.consistent,
                "issues": r.issues,
            }
            for r in records
        ]
        for flavour, records in records_by_flavour.items()
    }
    console.print(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_list_verb(cfg: Config, opts: ListOptions, *,
                  docker: DockerCLI | None = None,
                  console: Console | None = None,
                  err_console: Console | None = None) -> int:
    """Execute the `list` verb. Always exits 0 — `list` is informational, not a check."""
    console = console or Console()
    err_console = err_console or Console(stderr=True)
    docker = docker or DockerCLI()

    flavour_names: list[str]
    if opts.flavour is None:
        flavour_names = sorted(cfg.flavours)
    else:
        cfg.require_flavour(opts.flavour)
        flavour_names = [opts.flavour]

    matrix = load_matrix_for_root(cfg)

    records_by_flavour: dict[str, list[ImageRecord]] = {}
    for name in flavour_names:
        flavour = cfg.flavours[name]
        versions = all_known_image_versions(cfg, flavour, matrix)
        records_by_flavour[name] = [
            build_image_record(cfg, flavour, v, matrix=matrix, docker=docker,
                               check_ghcr=opts.check_ghcr)
            for v in versions
        ]

    if opts.json_output:
        _render_json(console, records_by_flavour)
    else:
        _render_tables(console, records_by_flavour)

    return exit_codes.OK
