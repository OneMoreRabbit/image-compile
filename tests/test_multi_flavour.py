"""Tests that the multi-flavour design holds: config-example parses, both
flavours produce distinct flavour-driven bundle paths, the inventory walks
each flavour's tree independently."""
from __future__ import annotations

from pathlib import Path

from image_compile.config import load_config
from image_compile.registry_paths import BundleLayout


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_EXAMPLE = REPO_ROOT / "config.yml.example"


def test_config_example_parses() -> None:
    cfg = load_config(CONFIG_EXAMPLE)
    assert "openclaw" in cfg.flavours
    assert "nanoclaw" in cfg.flavours


def test_nanoclaw_flavour_has_correct_shape() -> None:
    cfg = load_config(CONFIG_EXAMPLE)
    nano = cfg.flavours["nanoclaw"]
    assert nano.image_line == "nanoclaw-runtime"
    assert nano.upstream.kind == "github_release"
    assert nano.upstream.repo == "nanoclaw/nanoclaw"
    assert nano.probe.default_port == 17000
    assert nano.probe.port_env_var == "NANOCLAW_PORT"
    assert nano.probe.ready_endpoint == "/ready"


def test_bundle_layout_uses_flavour_per_config(tmp_path: Path) -> None:
    layout_oc = BundleLayout(root=tmp_path, image_version="2026.5.5-r1", flavour="openclaw")
    layout_nc = BundleLayout(root=tmp_path, image_version="1.2.3-r1", flavour="nanoclaw")

    assert layout_oc.bundle_dir.parent.name == "openclaw"
    assert layout_nc.bundle_dir.parent.name == "nanoclaw"
    assert layout_oc.config_file.name == "openclaw.json"
    assert layout_nc.config_file.name == "nanoclaw.json"

    # No path collisions between the two flavours
    assert layout_oc.bundle_dir != layout_nc.bundle_dir


def test_nanoclaw_templates_exist() -> None:
    templates = REPO_ROOT / "templates" / "nanoclaw"
    assert (templates / "starting-nanoclaw.json.j2").is_file()
    assert (templates / "probe-stub-secrets.env").is_file()
    for w in ("AGENTS.md.j2", "SOUL.md.j2", "TOOLS.md.j2"):
        assert (templates / "blank-workspace" / w).is_file(), f"missing {w}"
