"""Defaults bundle assembly.

After the probe runs, take what we captured and produce the persistent
artifacts under ~/registry/image_defaults/<flavour>/<image_version>/.

Steps (per parent brief §The build workflow step 5 + Amendments):
  - Scrub the captured openclaw.json: replace stub secret values with SecretRef
    shapes, drop probe-specific identity values, sort keys, 2-space indent.
  - Render the blank-workspace markdown templates.
  - Write the probe report.
  - Write metadata.yml.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import __version__ as TOOL_VERSION
from .config import FlavourConfig
from .probe_report import ProbeReport
from .registry_paths import BundleLayout


# Stub secret values that appear in our probe-stub-secrets.env and may have
# landed inside the captured openclaw.json. The scrub maps them back to
# SecretRef shapes pointing at named env vars.
DEFAULT_STUB_SECRETS: dict[str, str] = {
    "stub-discord-token-not-real": "DISCORD_BOT_TOKEN",
    "stub-model-key-not-real": "STUB_API_KEY",
    "stub-gateway-token-not-real": "OPENCLAW_GATEWAY_TOKEN",
}

# Probe-identity values that should not leak into a defaults bundle.
PROBE_IDENTITY_KEYS = ("name", "AGENT_NAME")
PROBE_IDENTITY_VALUE = "probe"


@dataclass(frozen=True)
class BundleMetadata:
    image_tag: str
    image_version: str
    flavour: str
    upstream_version: str
    wrapper_revision: str
    wrapper_repo_head: str | None
    image_compile_version: str
    build_date: datetime
    built_by: str
    built_on: str
    notes: str = ""
    baked_plugins: tuple[str, ...] = ()      # pinned specs baked into the image

    def to_yaml_dict(self) -> dict:
        return {
            "image_tag": self.image_tag,
            "image_version": self.image_version,
            "flavour": self.flavour,
            "upstream_version": self.upstream_version,
            "wrapper_revision": self.wrapper_revision,
            "wrapper_repo_head": self.wrapper_repo_head,
            "image_compile_version": self.image_compile_version,
            "build_date": self.build_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "built_by": self.built_by,
            "built_on": self.built_on,
            "baked_plugins": list(self.baked_plugins),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# openclaw.json scrubbing
# ---------------------------------------------------------------------------

def scrub_openclaw_config(captured: dict, *,
                          stub_secrets: dict[str, str] | None = None,
                          probe_identity: str = PROBE_IDENTITY_VALUE,
                          drop_channels: tuple[str, ...] = ()) -> dict:
    """Return a scrubbed copy of the captured openclaw.json.

    - Any string whose value matches a known stub-secret token is replaced
      with a SecretRef object: {"source": "env", "id": "<NAME>"}.
    - Any string equal to the probe identity ("probe") is dropped (key kept,
      value set to empty string) so the defaults don't carry that name.
    - `drop_channels` entries are removed from `channels` wholesale: the
      probe injects `channels.<id>.enabled: true` for the channel-start
      check (guard 8), and a channel enabled in image defaults would enable
      it for every compiled agent.
    - Output keys are sorted (handled by the caller during JSON dump).
    """
    stub_secrets = stub_secrets if stub_secrets is not None else DEFAULT_STUB_SECRETS

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if isinstance(node, str):
            if node in stub_secrets:
                return {"source": "env", "provider": "default", "id": stub_secrets[node]}
            if node == probe_identity:
                return ""
            return node
        return node

    scrubbed = _walk(deepcopy(captured))
    channels = scrubbed.get("channels")
    if isinstance(channels, dict):
        for cid in drop_channels:
            channels.pop(cid, None)
        if not channels:
            scrubbed.pop("channels", None)
    return scrubbed


def dump_openclaw_config(scrubbed: dict) -> str:
    """Serialise the scrubbed config with sorted keys and 2-space indent.

    Keys are sorted at every level for deterministic diffs across builds.
    """
    return json.dumps(scrubbed, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Workspace rendering
# ---------------------------------------------------------------------------

def render_workspace(flavour: FlavourConfig, *, templates_root: Path,
                     target_dir: Path, image_version: str, upstream_version: str,
                     build_date: datetime) -> list[Path]:
    """Render the blank-workspace markdown templates for this flavour into target_dir.

    Returns the list of paths written.
    """
    env = Environment(
        loader=FileSystemLoader(str(templates_root)),
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    workspace_root = templates_root / flavour.workspace_templates_dir
    if not workspace_root.is_dir():
        raise FileNotFoundError(
            f"workspace templates dir missing: {workspace_root}"
        )

    ctx = {
        "image_version": image_version,
        "upstream_version": upstream_version,
        "build_date": build_date.strftime("%Y-%m-%d"),
    }

    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for tpl_path in sorted(workspace_root.iterdir()):
        if not tpl_path.is_file() or not tpl_path.name.endswith(".md.j2"):
            continue
        rel = tpl_path.relative_to(templates_root).as_posix()
        rendered = env.get_template(rel).render(**ctx)
        out_name = tpl_path.name[: -len(".j2")]              # AGENTS.md.j2 → AGENTS.md
        out_path = target_dir / out_name
        out_path.write_text(rendered, encoding="utf-8")
        written.append(out_path)
    return written


# ---------------------------------------------------------------------------
# Top-level bundle assembly
# ---------------------------------------------------------------------------

def write_bundle(layout: BundleLayout, flavour: FlavourConfig, *,
                 captured_openclaw_json: dict, probe_report: ProbeReport,
                 metadata: BundleMetadata, templates_root: Path) -> list[Path]:
    """Write every file in the defaults bundle for this image version.

    Returns the list of paths created (useful for the build summary).
    """
    written: list[Path] = []

    # openclaw.json (scrubbed; the guard-8 probe-injected channel must not
    # become an image default)
    drop = (flavour.probe.channel_start_check,) if flavour.probe.channel_start_check else ()
    scrubbed = scrub_openclaw_config(captured_openclaw_json, drop_channels=drop)
    layout.bundle_dir.mkdir(parents=True, exist_ok=True)
    layout.config_file.write_text(dump_openclaw_config(scrubbed), encoding="utf-8")
    written.append(layout.config_file)

    # workspace markdown
    written.extend(render_workspace(
        flavour,
        templates_root=templates_root,
        target_dir=layout.workspace_dir,
        image_version=layout.image_version,
        upstream_version=metadata.upstream_version,
        build_date=metadata.build_date,
    ))

    # probe-report.yml
    layout.probe_report.write_text(
        yaml.safe_dump(probe_report.to_yaml_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    written.append(layout.probe_report)

    # metadata.yml
    layout.metadata_file.write_text(
        yaml.safe_dump(metadata.to_yaml_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    written.append(layout.metadata_file)

    return written


def build_metadata(*, image_tag: str, image_version: str, flavour_name: str,
                   upstream_version: str, wrapper_rev: str,
                   wrapper_repo_head: str | None, build_date: datetime,
                   built_by: str, built_on: str, notes: str = "",
                   baked_plugins: tuple[str, ...] = ()) -> BundleMetadata:
    return BundleMetadata(
        image_tag=image_tag,
        image_version=image_version,
        flavour=flavour_name,
        upstream_version=upstream_version,
        wrapper_revision=wrapper_rev,
        wrapper_repo_head=wrapper_repo_head,
        image_compile_version=TOOL_VERSION,
        build_date=build_date,
        built_by=built_by,
        built_on=built_on,
        notes=notes,
        baked_plugins=baked_plugins,
    )
