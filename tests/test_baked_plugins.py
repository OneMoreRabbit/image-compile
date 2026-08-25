"""Tests for the baked-plugins path (r8 bundled model): build argv, bundle
metadata, the stub template emitting NO plugin config, and the probe's
stock-root assertion."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from image_compile.bundle import build_metadata, scrub_openclaw_config
from image_compile.config import plugin_id
from image_compile.docker_build import BuildPlan, _construct_argv
from image_compile.probe import find_plugin_load_errors, missing_bundled_plugins

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


def _render_stub(**extra) -> dict:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_ROOT)),
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    ctx = {
        "AGENT_NAME": "probe",
        "OPENCLAW_BIND": "lan",
        "OPENCLAW_PORT": 18789,
        "image_compile_version": "0.0.0",
        "build_date": "2026-07-09T00:00:00Z",
        "channel_start_check": None,
    }
    ctx.update(extra)
    rendered = env.get_template("openclaw/starting-openclaw.json.j2").render(**ctx)
    return json.loads(rendered)          # must be valid JSON either way


def test_stub_template_emits_no_plugin_config() -> None:
    """r8: bundled plugins need no discovery config — the stub must not
    contain a plugins block at all (stale load.paths caused the r7
    duplicate-discovery warning)."""
    cfg = _render_stub()
    assert "plugins" not in cfg
    assert "channels" not in cfg


def test_stub_template_enables_channel_start_check() -> None:
    cfg = _render_stub(channel_start_check="whatsapp")
    assert cfg["channels"] == {"whatsapp": {"enabled": True}}


def test_find_plugin_load_errors() -> None:
    logs = (
        "2026-07-09T10:00:00Z [gateway] listening\n"
        "2026-07-09T10:00:01Z [channels] failed to load persistedAuthState "
        "checker for whatsapp: plugin module path escapes plugin root or "
        "fails alias checks\n"
        "2026-07-09T10:00:02Z [ws] webchat connected\n"
    )
    errors = find_plugin_load_errors(logs)
    assert len(errors) == 1
    assert "escapes plugin root" in errors[0]
    assert find_plugin_load_errors("[gateway] listening\n") == []


def test_scrub_drops_probe_injected_channel() -> None:
    captured = {
        "gateway": {"port": 18789},
        "channels": {"whatsapp": {"enabled": True}},
    }
    scrubbed = scrub_openclaw_config(captured, drop_channels=("whatsapp",))
    # the whole channels block goes when the injected channel was its only key
    assert "channels" not in scrubbed
    # other channels survive
    captured["channels"]["discord"] = {"enabled": False}
    scrubbed = scrub_openclaw_config(captured, drop_channels=("whatsapp",))
    assert scrubbed["channels"] == {"discord": {"enabled": False}}


def test_missing_bundled_plugins_detects_absent_ids() -> None:
    listing = (
        "plugins:\n"
        "  whatsapp   stock:whatsapp/index.js   disabled\n"
        "  brave      stock:brave/index.js      disabled\n"
        "  telegram   stock:telegram/index.js   enabled\n"
    )
    assert missing_bundled_plugins(listing, ["whatsapp", "brave"]) == []
    assert missing_bundled_plugins(listing, ["whatsapp", "discord"]) == ["discord"]
    # a plugin visible from a NON-stock root does not count as bundled
    external = "  discord   /agent/configs/main/npm/projects/x/index.js  disabled\n"
    assert missing_bundled_plugins(listing + external, ["discord"]) == ["discord"]
