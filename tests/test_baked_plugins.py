"""Tests for the r7 baked-plugins path: build argv, bundle metadata, and the
probe stub template carrying plugins.load.paths."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from image_compile.bundle import build_metadata
from image_compile.config import plugin_id
from image_compile.docker_build import BuildPlan, _construct_argv

TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "templates"


def _plan(baked: tuple[str, ...] = ()) -> BuildPlan:
    return BuildPlan(
        image_tag="ghcr.io/arcpower/openclaw-runtime:2026.6.11-r7",
        image_version="2026.6.11-r7",
        upstream_version_no_prefix="2026.6.11",
        wrapper_repo=Path("/tmp/openclaw-runtime"),
        wrapper_head_sha="abc1234",
        upstream_version="v2026.6.11",
        baked_plugins=baked,
    )


def test_plugin_id_rule() -> None:
    assert plugin_id("@openclaw/brave-plugin") == "brave"
    assert plugin_id("@openclaw/whatsapp") == "whatsapp"
    assert plugin_id("@openclaw/perplexity-plugin") == "perplexity"
    assert plugin_id("plainpkg") == "plainpkg"


def test_argv_includes_baked_plugins_build_arg_and_label() -> None:
    specs = ("@openclaw/whatsapp@2026.6.11", "@openclaw/brave-plugin@2026.6.11")
    argv = _construct_argv(_plan(specs), source="test")
    joined = " ".join(argv)
    assert "BAKED_PLUGINS=@openclaw/whatsapp@2026.6.11 @openclaw/brave-plugin@2026.6.11" in joined
    assert ("org.arcpower.openclaw.baked-plugins="
            "@openclaw/whatsapp@2026.6.11,@openclaw/brave-plugin@2026.6.11") in joined


def test_argv_omits_baked_plugins_when_empty() -> None:
    argv = _construct_argv(_plan(), source="test")
    joined = " ".join(argv)
    assert "BAKED_PLUGINS" not in joined
    assert "baked-plugins" not in joined


def test_metadata_records_baked_plugins() -> None:
    md = build_metadata(
        image_tag="t", image_version="2026.6.11-r7", flavour_name="openclaw",
        upstream_version="v2026.6.11", wrapper_rev="r7", wrapper_repo_head="abc",
        build_date=datetime(2026, 7, 8, tzinfo=timezone.utc),
        built_by="test", built_on="host",
        baked_plugins=("@openclaw/whatsapp@2026.6.11",),
    )
    d = md.to_yaml_dict()
    assert d["baked_plugins"] == ["@openclaw/whatsapp@2026.6.11"]


def _render_stub(baked_plugin_paths: list[str]) -> dict:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_ROOT)),
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    rendered = env.get_template("openclaw/starting-openclaw.json.j2").render(
        AGENT_NAME="probe",
        OPENCLAW_BIND="lan",
        OPENCLAW_PORT=18789,
        image_compile_version="0.0.0",
        build_date="2026-07-08T00:00:00Z",
        baked_plugin_paths=baked_plugin_paths,
    )
    return json.loads(rendered)          # must be valid JSON either way


def test_stub_template_carries_plugin_load_paths() -> None:
    paths = [
        "/opt/openclaw-plugins/whatsapp/node_modules/@openclaw/whatsapp",
        "/opt/openclaw-plugins/brave/node_modules/@openclaw/brave-plugin",
    ]
    cfg = _render_stub(paths)
    assert cfg["plugins"]["load"]["paths"] == paths


def test_stub_template_omits_plugins_block_when_empty() -> None:
    cfg = _render_stub([])
    assert "plugins" not in cfg
