"""Tests for the list/verify shared inventory helpers.

The Docker-side checks (GHCR existence) are exercised with a fake DockerCLI
so unit tests stay hermetic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from image_compile.config import Config, FlavourConfig, GhcrConfig, ProbeConfig, RegistryConfig, UpstreamConfig
from image_compile.docker_cli import DockerCLI, DockerResult
from image_compile.inventory import (
    build_image_record, check_ghcr_tag, discover_local_image_versions,
    inspect_bundle, lookup_self_blessed,
)
from image_compile.matrix import self_blessed_entry, upsert_entry, write_matrix, load_matrix
from image_compile.registry_paths import BundleLayout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flavour(name: str = "openclaw") -> FlavourConfig:
    return FlavourConfig(
        name=name,
        image_line=f"{name}-runtime",
        wrapper_repo_default=Path(f"/tmp/{name}-runtime"),
        upstream=UpstreamConfig(kind="github_release", repo=f"{name}/{name}", version_strip_prefix="v"),
        probe=ProbeConfig(
            stub_config_template=f"{name}/starting-{name}.json.j2",
            stub_secrets_template=f"{name}/probe-stub-secrets.env",
            startup_timeout_seconds=30, settle_seconds=5,
            health_endpoint="/healthz", ready_endpoint="/readyz",
            port_env_var=f"{name.upper()}_PORT", default_port=18789,
        ),
        workspace_templates_dir=Path(f"{name}/blank-workspace"),
    )


def _cfg(tmp_path: Path, flavours: dict[str, FlavourConfig]) -> Config:
    return Config(
        ghcr=GhcrConfig(org="arcpower"),
        registry=RegistryConfig(root=tmp_path, archive_root=tmp_path / ".archive"),
        flavours=flavours,
    )


def _populate_bundle(tmp_path: Path, flavour: str, version: str) -> BundleLayout:
    layout = BundleLayout(root=tmp_path, image_version=version, flavour=flavour)
    layout.bundle_dir.mkdir(parents=True, exist_ok=True)
    layout.workspace_dir.mkdir(exist_ok=True)
    layout.config_file.write_text("{}", encoding="utf-8")
    layout.probe_report.write_text("probe_metadata: {}\n", encoding="utf-8")
    layout.metadata_file.write_text("image_tag: x\n", encoding="utf-8")
    for w in ("AGENTS.md", "SOUL.md", "TOOLS.md"):
        (layout.workspace_dir / w).write_text(f"# {w}\n", encoding="utf-8")
    return layout


# ---------------------------------------------------------------------------
# discover_local_image_versions
# ---------------------------------------------------------------------------

def test_discover_local_image_versions_empty(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, {"openclaw": _flavour("openclaw")})
    assert discover_local_image_versions(cfg, "openclaw") == []


def test_discover_local_image_versions_sorted(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, {"openclaw": _flavour("openclaw")})
    _populate_bundle(tmp_path, "openclaw", "2026.5.8-r1")
    _populate_bundle(tmp_path, "openclaw", "2026.5.5-r1")
    # Hidden dir is ignored
    (tmp_path / "image_defaults" / "openclaw" / ".archive").mkdir(parents=True)
    assert discover_local_image_versions(cfg, "openclaw") == ["2026.5.5-r1", "2026.5.8-r1"]


# ---------------------------------------------------------------------------
# inspect_bundle
# ---------------------------------------------------------------------------

def test_inspect_bundle_missing(tmp_path: Path) -> None:
    layout = BundleLayout(root=tmp_path, image_version="x-r1", flavour="openclaw")
    assert inspect_bundle(layout).check == "missing"


def test_inspect_bundle_ok(tmp_path: Path) -> None:
    layout = _populate_bundle(tmp_path, "openclaw", "x-r1")
    status = inspect_bundle(layout)
    assert status.check == "ok"
    assert status.missing_files == ()


def test_inspect_bundle_incomplete(tmp_path: Path) -> None:
    layout = _populate_bundle(tmp_path, "openclaw", "x-r1")
    layout.metadata_file.unlink()
    (layout.workspace_dir / "SOUL.md").unlink()
    status = inspect_bundle(layout)
    assert status.check == "incomplete"
    assert "metadata.yml" in status.missing_files
    assert "workspace/SOUL.md" in status.missing_files


def test_inspect_bundle_uses_flavour_config_filename(tmp_path: Path) -> None:
    layout = _populate_bundle(tmp_path, "nanoclaw", "1.0-r1")
    # The config file should be nanoclaw.json, not openclaw.json
    assert layout.config_file.name == "nanoclaw.json"
    assert inspect_bundle(layout).check == "ok"


# ---------------------------------------------------------------------------
# lookup_self_blessed
# ---------------------------------------------------------------------------

def test_lookup_self_blessed_found(tmp_path: Path) -> None:
    matrix_path = tmp_path / "compatibility_matrix.yml"
    matrix = load_matrix(matrix_path)
    entry = self_blessed_entry(
        "openclaw", "2026.5.5-r1",
        tested_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
    )
    upsert_entry(matrix, entry)
    write_matrix(matrix_path, matrix)

    reloaded = load_matrix(matrix_path)
    status = lookup_self_blessed(reloaded, "openclaw", "2026.5.5-r1")
    assert status.check == "blessed"
    assert status.tested_at == "2026-05-14T00:00:00Z"


def test_lookup_self_blessed_missing(tmp_path: Path) -> None:
    matrix = load_matrix(tmp_path / "compatibility_matrix.yml")
    status = lookup_self_blessed(matrix, "openclaw", "nope")
    assert status.check == "missing"


# ---------------------------------------------------------------------------
# check_ghcr_tag — with a fake Docker runner
# ---------------------------------------------------------------------------

def _fake_runner(returncode: int, stderr: str = "", stdout: str = ""):
    def runner(argv, *, check, capture_output, timeout):
        return DockerResult(argv=argv, returncode=returncode, stdout=stdout, stderr=stderr)
    return runner


def test_check_ghcr_tag_present() -> None:
    docker = DockerCLI(runner=_fake_runner(returncode=0, stdout='{"mediaType":"..."}'))
    status = check_ghcr_tag(docker, "ghcr.io/x/y:1")
    assert status.check == "present"


def test_check_ghcr_tag_absent() -> None:
    docker = DockerCLI(runner=_fake_runner(returncode=1, stderr="manifest unknown"))
    status = check_ghcr_tag(docker, "ghcr.io/x/y:1")
    assert status.check == "absent"


def test_check_ghcr_tag_unauthorised() -> None:
    docker = DockerCLI(runner=_fake_runner(
        returncode=1, stderr="unauthorized: authentication required",
    ))
    status = check_ghcr_tag(docker, "ghcr.io/x/y:1")
    assert status.check == "unauthorised"


def test_check_ghcr_tag_unknown_failure_marked_unchecked() -> None:
    docker = DockerCLI(runner=_fake_runner(returncode=1, stderr="network unreachable"))
    status = check_ghcr_tag(docker, "ghcr.io/x/y:1")
    assert status.check == "unchecked"


# ---------------------------------------------------------------------------
# build_image_record — integration of the three checks
# ---------------------------------------------------------------------------

def test_build_image_record_consistent(tmp_path: Path) -> None:
    flavour = _flavour("openclaw")
    cfg = _cfg(tmp_path, {"openclaw": flavour})
    _populate_bundle(tmp_path, "openclaw", "2026.5.5-r1")

    matrix_path = tmp_path / "compatibility_matrix.yml"
    matrix = load_matrix(matrix_path)
    upsert_entry(matrix, self_blessed_entry(
        "openclaw", "2026.5.5-r1",
        tested_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
    ))
    write_matrix(matrix_path, matrix)
    reloaded = load_matrix(matrix_path)

    docker = DockerCLI(runner=_fake_runner(returncode=0))
    rec = build_image_record(cfg, flavour, "2026.5.5-r1",
                             matrix=reloaded, docker=docker, check_ghcr=True)
    assert rec.consistent is True
    assert rec.issues == []
    assert rec.image_tag == "ghcr.io/arcpower/openclaw-runtime:2026.5.5-r1"


def test_build_image_record_missing_bundle(tmp_path: Path) -> None:
    flavour = _flavour("openclaw")
    cfg = _cfg(tmp_path, {"openclaw": flavour})
    # No bundle on disk; matrix has the entry though.
    matrix_path = tmp_path / "compatibility_matrix.yml"
    matrix = load_matrix(matrix_path)
    upsert_entry(matrix, self_blessed_entry(
        "openclaw", "2026.5.5-r1",
        tested_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
    ))
    write_matrix(matrix_path, matrix)
    reloaded = load_matrix(matrix_path)

    rec = build_image_record(cfg, flavour, "2026.5.5-r1",
                             matrix=reloaded, check_ghcr=False)
    assert rec.consistent is False
    assert any("bundle dir missing" in i for i in rec.issues)


def test_build_image_record_missing_matrix(tmp_path: Path) -> None:
    flavour = _flavour("openclaw")
    cfg = _cfg(tmp_path, {"openclaw": flavour})
    _populate_bundle(tmp_path, "openclaw", "2026.5.5-r1")
    matrix = load_matrix(tmp_path / "compatibility_matrix.yml")
    rec = build_image_record(cfg, flavour, "2026.5.5-r1",
                             matrix=matrix, check_ghcr=False)
    assert rec.consistent is False
    assert "no matrix entry" in rec.issues
