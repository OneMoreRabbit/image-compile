"""Unit tests for the config loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from image_compile.config import ConfigError, load_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_OPENCLAW = """
ghcr:
  org: arcpower

registry:
  root: /tmp/test-registry

flavours:
  openclaw:
    image_line: openclaw-runtime
    wrapper_repo_default: /tmp/openclaw-runtime
    upstream:
      kind: github_release
      repo: openclaw/openclaw
      version_strip_prefix: "v"
    probe:
      stub_config_template: openclaw/starting-openclaw.json.j2
      stub_secrets_template: openclaw/probe-stub-secrets.env
      startup_timeout_seconds: 30
      settle_seconds: 5
      health_endpoint: /healthz
      ready_endpoint: /readyz
      port_env_var: OPENCLAW_PORT
      default_port: 18789
    workspace_templates_dir: openclaw/blank-workspace
"""


@pytest.fixture
def minimal_config(tmp_path: Path) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(MINIMAL_OPENCLAW, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_baked_plugins_default_empty(minimal_config: Path) -> None:
    f = load_config(minimal_config).flavours["openclaw"]
    assert f.baked_plugins == ()
    assert f.baked_plugin_ids == ()


def test_baked_plugins_parse_and_paths(tmp_path: Path) -> None:
    cfg_text = MINIMAL_OPENCLAW + """
"""
    cfg_text = cfg_text.replace(
        "    workspace_templates_dir: openclaw/blank-workspace",
        "    workspace_templates_dir: openclaw/blank-workspace\n"
        "    baked_plugins:\n"
        "      - \"@openclaw/whatsapp\"\n"
        "      - \"@openclaw/brave-plugin\"\n",
    )
    p = tmp_path / "config.yml"
    p.write_text(cfg_text, encoding="utf-8")
    f = load_config(p).flavours["openclaw"]
    assert f.baked_plugins == ("@openclaw/whatsapp", "@openclaw/brave-plugin")
    # id rule: basename after scope, minus -plugin suffix (r8: ids drive the
    # probe's stock-root assertion; no path config is emitted at all)
    assert f.baked_plugin_ids == ("whatsapp", "brave")


def test_baked_plugins_reject_pinned_specs(tmp_path: Path) -> None:
    cfg_text = MINIMAL_OPENCLAW.replace(
        "    workspace_templates_dir: openclaw/blank-workspace",
        "    workspace_templates_dir: openclaw/blank-workspace\n"
        "    baked_plugins: [\"@openclaw/whatsapp@2026.6.11\"]\n",
    )
    p = tmp_path / "config.yml"
    p.write_text(cfg_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="carries a version"):
        load_config(p)


def test_loads_minimal_config(minimal_config: Path) -> None:
    cfg = load_config(minimal_config)
    assert cfg.ghcr.org == "arcpower"
    assert cfg.registry.root == Path("/tmp/test-registry")
    assert cfg.registry.archive_root == Path("/tmp/test-registry/.archive")
    assert "openclaw" in cfg.flavours
    f = cfg.flavours["openclaw"]
    assert f.image_line == "openclaw-runtime"
    assert f.upstream.kind == "github_release"
    assert f.upstream.repo == "openclaw/openclaw"
    assert f.upstream.version_strip_prefix == "v"
    assert f.probe.startup_timeout_seconds == 30
    assert f.probe.port_env_var == "OPENCLAW_PORT"
    assert f.required_wrapper_files == ("Dockerfile", "entrypoint.sh")


def test_require_flavour_returns_flavour(minimal_config: Path) -> None:
    cfg = load_config(minimal_config)
    f = cfg.require_flavour("openclaw")
    assert f.name == "openclaw"


def test_require_flavour_unknown_raises(minimal_config: Path) -> None:
    cfg = load_config(minimal_config)
    with pytest.raises(ConfigError, match="nanoclaw"):
        cfg.require_flavour("nanoclaw")


def test_archive_root_override(tmp_path: Path) -> None:
    p = tmp_path / "config.yml"
    p.write_text(MINIMAL_OPENCLAW.replace(
        "  root: /tmp/test-registry\n",
        "  root: /tmp/test-registry\n  archive_root: /tmp/elsewhere\n",
    ), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.registry.archive_root == Path("/tmp/elsewhere")


def test_global_cleanup_flags_defaults(minimal_config: Path) -> None:
    cfg = load_config(minimal_config)
    assert cfg.cleanup_on_success is True
    assert cfg.cleanup_on_failure is False
    assert cfg.remove_local_image_on_failure is True


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yml")


def test_missing_top_level_key_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yml"
    p.write_text("ghcr:\n  org: arcpower\nregistry:\n  root: /tmp\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="flavours"):
        load_config(p)


def test_missing_flavour_key_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yml"
    p.write_text(MINIMAL_OPENCLAW.replace("    image_line: openclaw-runtime\n", ""),
                 encoding="utf-8")
    with pytest.raises(ConfigError, match="image_line"):
        load_config(p)


def test_invalid_upstream_kind_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yml"
    p.write_text(MINIMAL_OPENCLAW.replace("kind: github_release", "kind: ftp_pull"),
                 encoding="utf-8")
    with pytest.raises(ConfigError, match="kind"):
        load_config(p)


def test_github_release_without_repo_raises(tmp_path: Path) -> None:
    bad = MINIMAL_OPENCLAW.replace("      repo: openclaw/openclaw\n", "")
    p = tmp_path / "config.yml"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(ConfigError, match="repo"):
        load_config(p)


def test_empty_flavours_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yml"
    p.write_text(
        "ghcr:\n  org: arcpower\nregistry:\n  root: /tmp\nflavours: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="non-empty"):
        load_config(p)


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yml"
    p.write_text("ghcr: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="parse"):
        load_config(p)
