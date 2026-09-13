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


# ---------------------------------------------------------------------------
# Wrapper-revision guard (the r3 incident)
# ---------------------------------------------------------------------------

def _wrapper(tmp_path, changelog: str | None, commit: bool = True):
    """A throwaway git repo standing in for a wrapper checkout."""
    import subprocess
    repo = tmp_path / "wrapper"
    repo.mkdir()
    (repo / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    if changelog is not None:
        (repo / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    if commit:
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


CL_R8 = "# Changelog\n\nPreamble.\n\n## r8.1 — 2026-07-09\n\nfix\n\n## r8 — 2026-07-01\n\nolder\n"


def test_wrapper_rev_matching_changelog_passes(tmp_path) -> None:
    from image_compile.preflight import check_wrapper_rev
    check_wrapper_rev(_wrapper(tmp_path, CL_R8), "r8.1")


def test_wrapper_rev_mismatch_is_the_r3_incident(tmp_path) -> None:
    """--wrapper-rev r3 against a tree that is not r3 must fail the build."""
    from image_compile.preflight import PreflightError, check_wrapper_rev
    repo = _wrapper(tmp_path, "# Changelog\n\n## r1 — scaffold\n\nfirst\n")
    with pytest.raises(PreflightError) as ei:
        check_wrapper_rev(repo, "r3")
    assert "does not match the wrapper tree" in str(ei.value)
    assert "r1" in str(ei.value)


def test_dirty_wrapper_tree_refuses(tmp_path) -> None:
    from image_compile.preflight import PreflightError, check_wrapper_rev
    repo = _wrapper(tmp_path, CL_R8)
    (repo / "entrypoint.sh").write_text("# uncommitted\n", encoding="utf-8")
    with pytest.raises(PreflightError) as ei:
        check_wrapper_rev(repo, "r8.1")
    assert "uncommitted changes" in str(ei.value)


def test_dirty_wrapper_tree_allowed_explicitly(tmp_path) -> None:
    from image_compile.preflight import check_wrapper_rev
    repo = _wrapper(tmp_path, CL_R8)
    (repo / "entrypoint.sh").write_text("# uncommitted\n", encoding="utf-8")
    check_wrapper_rev(repo, "r8.1", allow_dirty=True)


def test_missing_changelog_fails_closed(tmp_path) -> None:
    """No evidence must not read as no problem -- and this is the branch that
    would have caught the real r3 incident.

    Verified against the actual history: the mislabelled image was built from
    `655d9ca`, and at that commit openclaw-runtime had NO CHANGELOG at all. The
    mismatch branch below would not have fired -- there was nothing to mismatch.
    Only failing closed on absent evidence catches it. A warn-and-continue here,
    which is the friendlier choice, would have let the r3 build through.
    """
    from image_compile.preflight import PreflightError, check_wrapper_rev
    with pytest.raises(PreflightError) as ei:
        check_wrapper_rev(_wrapper(tmp_path, None), "r8.1")
    assert "wrapper_rev_check" in str(ei.value)


def test_missing_changelog_passes_when_flavour_opts_out(tmp_path) -> None:
    from image_compile.preflight import check_wrapper_rev
    check_wrapper_rev(_wrapper(tmp_path, None), "r8.1", require_changelog=False)


def test_non_git_wrapper_fails_closed(tmp_path) -> None:
    """An undeterminable clean-check is a failure, not a pass."""
    from image_compile.preflight import PreflightError, check_wrapper_rev
    plain = tmp_path / "notgit"
    plain.mkdir()
    (plain / "CHANGELOG.md").write_text(CL_R8, encoding="utf-8")
    with pytest.raises(PreflightError) as ei:
        check_wrapper_rev(plain, "r8.1")
    assert "cannot determine" in str(ei.value)


# ---------------------------------------------------------------------------
# Constitution 11 -- behaviour-governing values are declared, or the tool fails
# ---------------------------------------------------------------------------

def _example_minus(tmp_path, section: str, key: str) -> Path:
    import copy, yaml
    root = Path(__file__).resolve().parent.parent
    raw = yaml.safe_load((root / "config.yml.example").read_text(encoding="utf-8"))
    cfg = copy.deepcopy(raw)
    del cfg["flavours"]["openclaw"][section][key]
    out = tmp_path / "cfg.yml"
    out.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return out


def test_version_strip_prefix_must_be_declared(tmp_path) -> None:
    """It shapes the image TAG, and a wrong tag is the r3 class. "" is the most
    plausible default there is, which is precisely why it may not be one."""
    with pytest.raises(ConfigError) as ei:
        load_config(_example_minus(tmp_path, "upstream", "version_strip_prefix"))
    assert "version_strip_prefix" in str(ei.value)
    assert "flavours.openclaw.upstream" in str(ei.value)


def test_ready_endpoint_must_be_declared(tmp_path) -> None:
    """A wrong ready path that happens to return 200 is a false `ready` -- the
    tool cannot tell it is probing the wrong door. A target, never defaulted."""
    with pytest.raises(ConfigError) as ei:
        load_config(_example_minus(tmp_path, "probe", "ready_endpoint"))
    assert "ready_endpoint" in str(ei.value)
    assert "flavours.openclaw.probe" in str(ei.value)
