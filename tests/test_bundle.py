"""Tests for openclaw.json scrubbing and bundle assembly."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from image_compile.bundle import (
    DEFAULT_STUB_SECRETS, build_metadata, dump_openclaw_config, render_workspace,
    scrub_openclaw_config,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_ROOT = PROJECT_ROOT / "templates"


# ---------------------------------------------------------------------------
# scrub_openclaw_config
# ---------------------------------------------------------------------------

def test_scrub_replaces_stub_secret_strings() -> None:
    captured = {
        "channels": {
            "discord": {
                "token": "stub-discord-token-not-real",
                "name": "probe",
            }
        }
    }
    out = scrub_openclaw_config(captured)
    assert out == {
        "channels": {
            "discord": {
                "token": {"source": "env", "provider": "default", "id": "DISCORD_BOT_TOKEN"},
                "name": "",
            }
        }
    }


def test_scrub_does_not_touch_unrelated_strings() -> None:
    captured = {"agent": {"name": "marketing-arc", "model": "claude-opus-4-7"}}
    out = scrub_openclaw_config(captured)
    assert out == captured


def test_scrub_preserves_non_string_values() -> None:
    captured = {"gateway": {"port": 18789, "enabled": True, "tags": ["lan"]}}
    out = scrub_openclaw_config(captured)
    assert out == captured


def test_scrub_walks_arrays() -> None:
    captured = {"allow": ["stub-model-key-not-real", "real-value"]}
    out = scrub_openclaw_config(captured)
    assert out == {
        "allow": [
            {"source": "env", "provider": "default", "id": "STUB_API_KEY"},
            "real-value",
        ]
    }


def test_scrub_does_not_mutate_input() -> None:
    captured = {"a": "stub-discord-token-not-real"}
    original = json.dumps(captured)
    _ = scrub_openclaw_config(captured)
    assert json.dumps(captured) == original


def test_default_stub_secrets_cover_all_stub_values() -> None:
    # Sanity: the scrub map must match the strings the probe-stub-secrets.env ships.
    secrets_file = TEMPLATES_ROOT / "openclaw" / "probe-stub-secrets.env"
    body = secrets_file.read_text(encoding="utf-8")
    for stub_value in DEFAULT_STUB_SECRETS:
        assert stub_value in body, f"{stub_value!r} should appear in {secrets_file}"


# ---------------------------------------------------------------------------
# dump_openclaw_config — deterministic output
# ---------------------------------------------------------------------------

def test_dump_sorts_keys_and_uses_two_space_indent() -> None:
    data = {"b": 1, "a": {"y": 2, "x": 1}}
    out = dump_openclaw_config(data)
    assert out == (
        '{\n'
        '  "a": {\n'
        '    "x": 1,\n'
        '    "y": 2\n'
        '  },\n'
        '  "b": 1\n'
        '}\n'
    )


# ---------------------------------------------------------------------------
# render_workspace
# ---------------------------------------------------------------------------

def test_render_workspace_writes_all_md_files(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from image_compile.config import FlavourConfig, ProbeConfig, UpstreamConfig

    flavour = FlavourConfig(
        name="openclaw",
        image_line="openclaw-runtime",
        wrapper_repo_default=tmp_path,
        upstream=UpstreamConfig(kind="github_release", repo="x/y", version_strip_prefix="v"),
        probe=ProbeConfig(
            stub_config_template="openclaw/starting-openclaw.json.j2",
            stub_secrets_template="openclaw/probe-stub-secrets.env",
            startup_timeout_seconds=30,
            settle_seconds=5,
            health_endpoint="/healthz",
            ready_endpoint="/readyz",
            port_env_var="OPENCLAW_PORT",
            default_port=18789,
        ),
        workspace_templates_dir=Path("openclaw/blank-workspace"),
    )

    target = tmp_path / "workspace"
    written = render_workspace(
        flavour,
        templates_root=TEMPLATES_ROOT,
        target_dir=target,
        image_version="2026.5.5-r1",
        upstream_version="v2026.5.5",
        build_date=datetime(2026, 5, 14, tzinfo=timezone.utc),
    )
    names = sorted(p.name for p in written)
    assert names == ["AGENTS.md", "SOUL.md", "TOOLS.md"]

    body = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert "2026.5.5-r1" in body
    assert "v2026.5.5" in body
    assert "2026-05-14" in body


# ---------------------------------------------------------------------------
# build_metadata — yaml shape
# ---------------------------------------------------------------------------

def test_build_metadata_yaml_dict_is_complete() -> None:
    from datetime import datetime, timezone

    md = build_metadata(
        image_tag="ghcr.io/arcpower/openclaw-runtime:2026.5.5-r1",
        image_version="2026.5.5-r1",
        flavour_name="openclaw",
        upstream_version="v2026.5.5",
        wrapper_rev="r1",
        wrapper_repo_head="abc123",
        build_date=datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc),
        built_by="docdoc",
        built_on="zaphod",
        notes="phase-1 verification",
    )
    out = md.to_yaml_dict()
    assert out["image_tag"] == "ghcr.io/arcpower/openclaw-runtime:2026.5.5-r1"
    assert out["flavour"] == "openclaw"
    assert out["wrapper_repo_head"] == "abc123"
    assert out["build_date"] == "2026-05-14T10:00:00Z"
    assert out["built_on"] == "zaphod"
    # Yaml-roundtrip check: dump+load returns the same shape (strings).
    assert yaml.safe_load(yaml.safe_dump(out)) == out
