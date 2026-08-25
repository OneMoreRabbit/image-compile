"""Configuration loader for image-compile.

Reads config.yml, validates the multi-flavour structure described in
docs/image-compile-brief-amendments-v0_1.md Amendment 1, and exposes a typed
view that the rest of the tool consumes.

The loader is intentionally strict at the surface: unknown top-level keys
warn but pass; missing required keys raise. This keeps config drift visible
without making every new optional knob a breaking change.
"""
from __future__ import annotations

import getpass
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _default_probe_dir_root() -> Path:
    """Per-user probe scratch root.

    The probe creates ephemeral surface-mount directories here. The root must
    be per-user: a shared `/tmp/image-compile` created by one user is mode 0755
    and unwritable by another, so two operators on the same build host would
    collide. Keying on the username sidesteps that entirely.
    """
    try:
        user = getpass.getuser()
    except Exception:                          # getuser can raise if no passwd entry
        user = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"image-compile-{user}"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Raised when config.yml is missing, malformed, or omits a required key."""


# ---------------------------------------------------------------------------
# Required-key schema
# ---------------------------------------------------------------------------

_REQUIRED_TOP_LEVEL = ("ghcr", "registry", "flavours")
_REQUIRED_GHCR = ("org",)
_REQUIRED_REGISTRY = ("root",)
_REQUIRED_FLAVOUR = (
    "image_line",
    "wrapper_repo_default",
    "upstream",
    "probe",
    "workspace_templates_dir",
)
_REQUIRED_UPSTREAM = ("kind",)
_REQUIRED_PROBE = (
    "stub_config_template",
    "stub_secrets_template",
    "startup_timeout_seconds",
    "settle_seconds",
    "health_endpoint",
    "port_env_var",
    "default_port",
)


# ---------------------------------------------------------------------------
# Typed views
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UpstreamConfig:
    kind: str                         # "github_release" | "npm" | "vendored"
    repo: str | None = None           # github_release: "openclaw/openclaw"
    version_strip_prefix: str = ""    # e.g. "v" → tag "v2026.5.5" becomes "2026.5.5"


@dataclass(frozen=True)
class ProbeConfig:
    stub_config_template: str
    stub_secrets_template: str
    startup_timeout_seconds: int
    settle_seconds: int
    health_endpoint: str
    ready_endpoint: str
    port_env_var: str
    default_port: int
    # Guard 8 (r8.1): channel id the probe stub enables so the boot exercises
    # channel start — plugin submodule loads only happen on that path. The
    # probe asserts plugin LOAD success (log markers), never connectivity
    # (stub creds cannot connect). None disables the check.
    channel_start_check: str | None = None


def plugin_id(package: str) -> str:
    """Derive a plugin id from its npm package name, mirroring the wrapper
    Dockerfile's rule: basename after any scope, minus a `-plugin` suffix.
    `@openclaw/brave-plugin` → `brave`; `@openclaw/whatsapp` → `whatsapp`."""
    base = package.rsplit("/", 1)[-1]
    return base[: -len("-plugin")] if base.endswith("-plugin") else base


@dataclass(frozen=True)
class FlavourConfig:
    name: str
    image_line: str                                 # "openclaw-runtime"
    wrapper_repo_default: Path
    upstream: UpstreamConfig
    probe: ProbeConfig
    workspace_templates_dir: Path                   # relative to package templates root
    required_wrapper_files: tuple[str, ...] = ("Dockerfile", "entrypoint.sh")
    baked_plugins: tuple[str, ...] = ()             # npm package names, UNpinned; the
                                                    # build pins each to the upstream
                                                    # version (lockstep releases)

    @property
    def baked_plugin_ids(self) -> tuple[str, ...]:
        """Runtime plugin ids for the baked set. r8: plugins bake as BUNDLED
        stock extensions (`dist/extensions/<id>/`), so no path config is
        emitted at all — the ids exist for the probe's layout assertion
        (each must appear under the stock source root in `plugins list`)."""
        return tuple(plugin_id(p) for p in self.baked_plugins)


@dataclass(frozen=True)
class GhcrConfig:
    org: str
    auth_method: str = "docker_config"


@dataclass(frozen=True)
class RegistryConfig:
    root: Path
    archive_root: Path


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file: Path | None = None


@dataclass(frozen=True)
class Config:
    """Top-level configuration view."""
    ghcr: GhcrConfig
    registry: RegistryConfig
    flavours: dict[str, FlavourConfig]
    probe_dir_root: Path = field(default_factory=_default_probe_dir_root)
    cleanup_on_success: bool = True
    cleanup_on_failure: bool = False
    remove_local_image_on_failure: bool = True
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    source_path: Path | None = None

    def require_flavour(self, name: str) -> FlavourConfig:
        """Return the named flavour or raise ConfigError if it isn't configured."""
        if name not in self.flavours:
            available = ", ".join(sorted(self.flavours)) or "(none configured)"
            raise ConfigError(
                f"flavour {name!r} is not configured. Available: {available}"
            )
        return self.flavours[name]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _expand_path(value: str | os.PathLike[str]) -> Path:
    """Expand ~ and environment variables, return Path."""
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def _require_keys(node: dict[str, Any], required: tuple[str, ...], where: str) -> None:
    missing = [k for k in required if k not in node]
    if missing:
        raise ConfigError(f"{where}: missing required key(s): {', '.join(missing)}")


def _parse_upstream(raw: dict[str, Any], flavour_name: str) -> UpstreamConfig:
    _require_keys(raw, _REQUIRED_UPSTREAM, f"flavours.{flavour_name}.upstream")
    kind = raw["kind"]
    if kind not in ("github_release", "npm", "vendored"):
        raise ConfigError(
            f"flavours.{flavour_name}.upstream.kind: must be one of "
            f"github_release | npm | vendored (got {kind!r})"
        )
    if kind == "github_release" and "repo" not in raw:
        raise ConfigError(
            f"flavours.{flavour_name}.upstream: github_release requires 'repo' (e.g. openclaw/openclaw)"
        )
    return UpstreamConfig(
        kind=kind,
        repo=raw.get("repo"),
        version_strip_prefix=raw.get("version_strip_prefix", ""),
    )


def _parse_probe(raw: dict[str, Any], flavour_name: str) -> ProbeConfig:
    _require_keys(raw, _REQUIRED_PROBE, f"flavours.{flavour_name}.probe")
    return ProbeConfig(
        stub_config_template=raw["stub_config_template"],
        stub_secrets_template=raw["stub_secrets_template"],
        startup_timeout_seconds=int(raw["startup_timeout_seconds"]),
        settle_seconds=int(raw["settle_seconds"]),
        health_endpoint=raw["health_endpoint"],
        ready_endpoint=raw.get("ready_endpoint", "/readyz"),
        port_env_var=raw["port_env_var"],
        default_port=int(raw["default_port"]),
        channel_start_check=raw.get("channel_start_check"),
    )


def _parse_flavour(name: str, raw: dict[str, Any]) -> FlavourConfig:
    _require_keys(raw, _REQUIRED_FLAVOUR, f"flavours.{name}")
    baked_raw = raw.get("baked_plugins", []) or []
    if not isinstance(baked_raw, list) or not all(isinstance(p, str) and p for p in baked_raw):
        raise ConfigError(f"flavours.{name}.baked_plugins: must be a list of npm package names")
    for pkg in baked_raw:
        if "@" in pkg.lstrip("@"):
            raise ConfigError(
                f"flavours.{name}.baked_plugins: {pkg!r} carries a version — list bare "
                f"package names; the build pins each to the upstream version"
            )
    return FlavourConfig(
        name=name,
        image_line=raw["image_line"],
        wrapper_repo_default=_expand_path(raw["wrapper_repo_default"]),
        upstream=_parse_upstream(raw["upstream"], name),
        probe=_parse_probe(raw["probe"], name),
        workspace_templates_dir=Path(raw["workspace_templates_dir"]),
        required_wrapper_files=tuple(raw.get("required_wrapper_files", ("Dockerfile", "entrypoint.sh"))),
        baked_plugins=tuple(baked_raw),
    )


def _default_config_path() -> Path | None:
    """Look for config.yml in the package install directory, then CWD."""
    candidates: list[Path] = []
    pkg_root = Path(__file__).resolve().parent.parent.parent
    candidates.append(pkg_root / "config.yml")
    candidates.append(Path.cwd() / "config.yml")
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_config(path: Path | None = None) -> Config:
    """Load and validate config from `path`, or look up the default location.

    Raises ConfigError on any parse, schema, or value problem.
    """
    if path is None:
        path = _default_config_path()
    if path is None:
        raise ConfigError(
            "no config.yml found. Pass --config <path>, or copy config.yml.example to config.yml."
        )
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: YAML parse error: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level must be a mapping")

    _require_keys(raw, _REQUIRED_TOP_LEVEL, str(path))

    _require_keys(raw["ghcr"], _REQUIRED_GHCR, "ghcr")
    ghcr = GhcrConfig(
        org=raw["ghcr"]["org"],
        auth_method=raw["ghcr"].get("auth_method", "docker_config"),
    )

    _require_keys(raw["registry"], _REQUIRED_REGISTRY, "registry")
    reg_root = _expand_path(raw["registry"]["root"])
    archive_root = _expand_path(raw["registry"].get("archive_root", reg_root / ".archive"))
    registry = RegistryConfig(root=reg_root, archive_root=archive_root)

    flavours_raw = raw["flavours"]
    if not isinstance(flavours_raw, dict) or not flavours_raw:
        raise ConfigError("flavours: must be a non-empty mapping of flavour-name to flavour-config")
    flavours = {name: _parse_flavour(name, body) for name, body in flavours_raw.items()}

    logging_raw = raw.get("logging", {}) or {}
    log_file = logging_raw.get("file")
    logging_cfg = LoggingConfig(
        level=logging_raw.get("level", "INFO"),
        file=_expand_path(log_file) if log_file else None,
    )

    return Config(
        ghcr=ghcr,
        registry=registry,
        flavours=flavours,
        probe_dir_root=(_expand_path(raw["probe_dir_root"])
                        if raw.get("probe_dir_root") else _default_probe_dir_root()),
        cleanup_on_success=bool(raw.get("cleanup_on_success", True)),
        cleanup_on_failure=bool(raw.get("cleanup_on_failure", False)),
        remove_local_image_on_failure=bool(raw.get("remove_local_image_on_failure", True)),
        logging=logging_cfg,
        source_path=path,
    )
