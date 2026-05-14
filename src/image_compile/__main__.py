"""CLI entry point for image-compile.

Phase 0 scaffold: all verbs are stubs that log a "not yet implemented" notice
and exit 0. Phase 1+ fills in the build orchestration, probe loop, bundle
assembly, registry interaction.
"""
from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console

from . import __version__
from . import exit_codes
from .build import BuildOptions, run_build_verb
from .config import ConfigError, load_config

console = Console()
err_console = Console(stderr=True)


def _stub(verb: str, **kwargs: object) -> None:
    """Phase 0 stub: report what the verb would do and exit 0."""
    console.print(f"[yellow]{verb}[/yellow] not yet implemented (Phase 0 scaffold)")
    if kwargs:
        console.print("  arguments:")
        for k, v in kwargs.items():
            console.print(f"    {k} = {v!r}")


# ---------------------------------------------------------------------------
# Common options shared across verbs
# ---------------------------------------------------------------------------

def common_options(f):
    """Decorator that attaches the global flags described in the parent brief."""
    f = click.option(
        "--config",
        "config_path",
        type=click.Path(dir_okay=False, path_type=Path),
        default=None,
        help="Path to config.yml (defaults to ./config.yml or package-bundled default).",
    )(f)
    f = click.option(
        "--registry-root",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Override the registry root (config.registry.root).",
    )(f)
    f = click.option("--json", "json_output", is_flag=True, help="Machine-readable JSON output.")(f)
    f = click.option("-v", "--verbose", is_flag=True, help="Log subprocess invocations.")(f)
    f = click.option("-q", "--quiet", is_flag=True, help="Errors only.")(f)
    return f


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="image-compile")
def cli() -> None:
    """Build pinned wrapper images and paired defaults bundles for agent-platform flavours."""


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

@cli.command("build")
@click.argument("flavour")
@click.argument("upstream_version")
@click.option("--wrapper-rev", default="r1", show_default=True, help="Wrapper revision suffix.")
@click.option("--wrapper-repo", type=click.Path(file_okay=False, path_type=Path),
              help="Path to the wrapper repo (overrides flavour default).")
@click.option("--no-push", is_flag=True, help="Build + probe + bundle locally; skip GHCR push.")
@click.option("--no-bundle", is_flag=True, help="Build + smoke only; skip probe and defaults bundle.")
@click.option("--force", is_flag=True, help="Overwrite existing GHCR tag and defaults bundle.")
@click.option("--no-validate-upstream", is_flag=True,
              help="Skip the GitHub release existence check.")
@click.option("--keep-on-failure", is_flag=True,
              help="Do not remove locally-loaded image on failure (overrides config default).")
@click.option("--dry-run", is_flag=True, help="Print what would happen; perform no side effects.")
@common_options
def build_cmd(flavour: str, upstream_version: str, wrapper_rev: str, wrapper_repo: Path | None,
              no_push: bool, no_bundle: bool, force: bool, no_validate_upstream: bool,
              keep_on_failure: bool, dry_run: bool, config_path: Path | None,
              registry_root: Path | None, json_output: bool, verbose: bool, quiet: bool) -> None:
    """Build, probe, bundle, and push a pinned wrapper image for FLAVOUR at UPSTREAM_VERSION."""
    try:
        cfg = load_config(config_path)
        cfg.require_flavour(flavour)
    except ConfigError as e:
        err_console.print(f"[red]config error:[/red] {e}")
        sys.exit(exit_codes.CONFIG_ERROR)

    if registry_root is not None:
        # Override the registry root for this invocation. Drag the archive root
        # along to the new root so --registry-root /tmp/x doesn't archive into
        # the operator's $HOME.
        from dataclasses import replace
        cfg = replace(cfg, registry=replace(
            cfg.registry, root=registry_root, archive_root=registry_root / ".archive"
        ))

    code = run_build_verb(cfg, BuildOptions(
        flavour=flavour,
        upstream_version=upstream_version,
        wrapper_rev=wrapper_rev,
        wrapper_repo=wrapper_repo,
        no_push=no_push,
        no_bundle=no_bundle,
        force=force,
        validate_upstream=not no_validate_upstream,
        keep_on_failure=keep_on_failure,
        dry_run=dry_run,
    ))
    sys.exit(code)


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

@cli.command("probe")
@click.argument("flavour")
@click.argument("image_tag")
@click.option("--pull", is_flag=True, help="Pull from GHCR before probing.")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, path_type=Path),
              help="Where to write probe results (default: stdout).")
@common_options
def probe_cmd(flavour: str, image_tag: str, pull: bool, output_path: Path | None,
              config_path: Path | None, registry_root: Path | None,
              json_output: bool, verbose: bool, quiet: bool) -> None:
    """Run the probe against an existing locally-loaded or GHCR image."""
    try:
        cfg = load_config(config_path)
        cfg.require_flavour(flavour)
    except ConfigError as e:
        err_console.print(f"[red]config error:[/red] {e}")
        sys.exit(exit_codes.CONFIG_ERROR)

    _stub("probe", flavour=flavour, image_tag=image_tag, pull=pull, output_path=output_path)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@cli.command("list")
@click.argument("flavour", required=False)
@common_options
def list_cmd(flavour: str | None, config_path: Path | None, registry_root: Path | None,
             json_output: bool, verbose: bool, quiet: bool) -> None:
    """Show built images known to GHCR, paired with their defaults bundles."""
    try:
        cfg = load_config(config_path)
        if flavour is not None:
            cfg.require_flavour(flavour)
    except ConfigError as e:
        err_console.print(f"[red]config error:[/red] {e}")
        sys.exit(exit_codes.CONFIG_ERROR)

    _stub("list", flavour=flavour)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

@cli.command("verify")
@click.argument("flavour")
@click.argument("image_version")
@common_options
def verify_cmd(flavour: str, image_version: str, config_path: Path | None,
               registry_root: Path | None, json_output: bool, verbose: bool, quiet: bool) -> None:
    """Cross-check image tag, defaults bundle, and matrix entry all exist and agree."""
    try:
        cfg = load_config(config_path)
        cfg.require_flavour(flavour)
    except ConfigError as e:
        err_console.print(f"[red]config error:[/red] {e}")
        sys.exit(exit_codes.CONFIG_ERROR)

    _stub("verify", flavour=flavour, image_version=image_version)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

@cli.command("diff")
@click.argument("flavour")
@click.argument("image_version_a")
@click.argument("image_version_b")
@common_options
def diff_cmd(flavour: str, image_version_a: str, image_version_b: str,
             config_path: Path | None, registry_root: Path | None,
             json_output: bool, verbose: bool, quiet: bool) -> None:
    """Show differences between two image versions' defaults bundles."""
    try:
        cfg = load_config(config_path)
        cfg.require_flavour(flavour)
    except ConfigError as e:
        err_console.print(f"[red]config error:[/red] {e}")
        sys.exit(exit_codes.CONFIG_ERROR)

    _stub("diff", flavour=flavour, a=image_version_a, b=image_version_b)


if __name__ == "__main__":
    cli()
