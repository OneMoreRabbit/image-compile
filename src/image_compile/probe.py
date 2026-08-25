"""Probe runner.

Boots a container from the just-built wrapper image with all four surface
mounts pre-populated, waits for the gateway to come up, observes filesystem
writes via `docker diff`, captures the post-boot openclaw.json, and produces
a ProbeReport for the defaults bundle.

See:
    - docs/image-compile-brief-amendments-v0_1.md §Amendment 4 (mount/env corrections)
    - docs/image-compile-brief-amendments-v0_1.md §Amendment 5 (write classification)
    - parent brief §The probe step
"""
from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import requests
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import exit_codes
from .config import FlavourConfig
from .docker_cli import DockerCLI, DockerError
from .probe_report import (
    DEFAULT_SURFACE_PREFIXES, HealthCheck, InContainerWriteRecord, ProbeReport,
    ProbeResult, SurfaceWriteRecord, classify_relocation, parse_docker_diff,
    partition_diff_by_surface,
)


class ProbeError(Exception):
    """Raised when the probe cannot complete. Carries the exit code."""

    def __init__(self, message: str, code: int = exit_codes.PROBE_FAILED) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ProbeSetup:
    flavour: FlavourConfig
    image_tag: str
    image_version: str
    probe_dir: Path                           # /tmp/image-compile/probe-<uuid>/
    container_name: str
    published_port: int
    agent_uid: int                            # host uid the container should run as
    agent_primary_gid: int
    templates_root: Path                      # image-compile/templates/

    @property
    def configs_dir(self) -> Path:
        return self.probe_dir / "configs"

    @property
    def memory_dir(self) -> Path:
        return self.probe_dir / "memory"

    @property
    def sessions_dir(self) -> Path:
        return self.probe_dir / "sessions"

    @property
    def scratch_dir(self) -> Path:
        return self.probe_dir / "scratch"


def _find_free_port(host: str = "127.0.0.1") -> int:
    """Bind ephemeral and return the chosen port. Race with the test, but
    cheap enough to be useful for this single-shot probe."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_probe_dirs(setup: ProbeSetup) -> None:
    """Pre-create the four surface dirs and the main/ subdir inside each."""
    for surface in DEFAULT_SURFACE_PREFIXES:
        (setup.probe_dir / surface / "main").mkdir(parents=True, exist_ok=True)
    setup.probe_dir.chmod(0o0755)


def render_stub_files(setup: ProbeSetup) -> None:
    """Render the probe stub openclaw.json + secrets.env into configs/main/."""
    env = Environment(
        loader=FileSystemLoader(str(setup.templates_root)),
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    from . import __version__ as TOOL_VERSION
    config_tpl = env.get_template(setup.flavour.probe.stub_config_template)
    rendered_config = config_tpl.render(
        AGENT_NAME="probe",
        OPENCLAW_BIND="lan",
        OPENCLAW_PORT=setup.flavour.probe.default_port,
        image_compile_version=TOOL_VERSION,
        build_date=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # r8: no plugin discovery config — baked plugins are BUNDLED stock
        # extensions the runtime finds on its own. The probe instead asserts
        # they appear under the stock source root (verify_bundled_plugins).
        # r8.1 guard 8: enabling one channel makes the boot exercise channel
        # start, where plugin submodule loads actually happen; the injected
        # block is scrubbed from the captured bundle config.
        channel_start_check=setup.flavour.probe.channel_start_check,
    )
    (setup.configs_dir / "main" / "openclaw.json").write_text(rendered_config, encoding="utf-8")

    secrets_src = setup.templates_root / setup.flavour.probe.stub_secrets_template
    secrets_dst = setup.configs_dir / "main" / "secrets.env"
    shutil.copyfile(secrets_src, secrets_dst)
    secrets_dst.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


# ---------------------------------------------------------------------------
# Container lifecycle
# ---------------------------------------------------------------------------

def _docker_run_argv(setup: ProbeSetup) -> list[str]:
    p = setup.flavour.probe
    return [
        "run", "-d", "--rm",
        "--name", setup.container_name,
        "-e", "AGENT_NAME=probe",
        "-e", f"AGENT_UID={setup.agent_uid}",
        "-e", f"AGENT_PRIMARY_GID={setup.agent_primary_gid}",
        "-e", "AGENT_SUPP_GIDS=",
        "-e", "AGENT_HOME=/agent",
        "-e", "OPENCLAW_BIND=lan",
        "-e", f"{p.port_env_var}={p.default_port}",
        "-v", f"{setup.configs_dir}:/agent/configs",
        "-v", f"{setup.memory_dir}:/agent/memory",
        "-v", f"{setup.sessions_dir}:/agent/sessions",
        "-v", f"{setup.scratch_dir}:/agent/scratch",
        "-p", f"127.0.0.1:{setup.published_port}:{p.default_port}",
        setup.image_tag,
    ]


def start_probe_container(docker: DockerCLI, setup: ProbeSetup) -> None:
    """Start the probe container detached. Raises ProbeError on failure."""
    try:
        docker.run(_docker_run_argv(setup))
    except DockerError as e:
        raise ProbeError(
            f"docker run failed:\n{e.stderr.strip() or e}\n"
            f"(see container logs at last-failed/)"
        ) from e


def _http_health(url: str, *, timeout: float = 4) -> HealthCheck:
    """One-shot HTTP probe. Returns a HealthCheck describing the response."""
    started = time.monotonic()
    try:
        resp = requests.get(url, timeout=timeout)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body = resp.text[:400] if resp.text else None
        return HealthCheck(status=resp.status_code, response_time_ms=elapsed_ms, body=body)
    except requests.RequestException as e:
        return HealthCheck(status=None, body=str(e)[:400])


def wait_for_health(setup: ProbeSetup, *, timeout_seconds: int,
                    progress: Callable[[str], None] | None = None) -> HealthCheck:
    """Poll the gateway's HTTP /healthz on the published port until 200 or timeout."""
    url = f"http://127.0.0.1:{setup.published_port}{setup.flavour.probe.health_endpoint}"
    deadline = time.monotonic() + timeout_seconds
    last: HealthCheck = HealthCheck(status=None)
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        last = _http_health(url)
        if last.status == 200:
            if progress:
                progress(f"healthz 200 after {attempts} attempts")
            return last
        time.sleep(1)
    if progress:
        progress(f"healthz: gave up after {attempts} attempts (last status={last.status})")
    return last


def check_readyz(setup: ProbeSetup) -> HealthCheck:
    """One-shot HTTP readiness probe."""
    url = f"http://127.0.0.1:{setup.published_port}{setup.flavour.probe.ready_endpoint}"
    return _http_health(url)


# The wrapper's r6 contract: OPENCLAW_STATE_DIR points at the configs
# surface. `docker exec` does not inherit entrypoint-exported vars, so the
# CLI invocation must set it explicitly or it derives state from $HOME.
EXEC_STATE_DIR = "/agent/configs/main"

DUPLICATE_PLUGIN_MARKER = "duplicate plugin id"

# Guard 8 (r8.1): markers of a plugin failing to LOAD (not to connect —
# stub creds can never connect, and connectivity is out of probe scope).
# Discovery-level checks passed while channel start failed on r8; these
# markers only appear once a channel-enabled boot exercises that path.
PLUGIN_LOAD_ERROR_MARKERS = (
    "pushPluginLoadError",
    "escapes plugin root",
    "fails alias checks",
)


def find_plugin_load_errors(container_logs: str) -> list[str]:
    """Return log lines indicating a plugin failed to load."""
    return [
        line for line in container_logs.splitlines()
        if any(marker in line for marker in PLUGIN_LOAD_ERROR_MARKERS)
    ]


def missing_bundled_plugins(plugins_list_output: str,
                            plugin_ids: Iterable[str]) -> list[str]:
    """Return the baked plugin ids that do NOT appear under the stock source
    root in `plugins list` output (stock plugins render as `stock:<id>/…`).

    dist/extensions/ is upstream-internal, not a published interface — this
    assertion is what turns an upstream layout change into a failed build
    instead of a broken deployed agent (r8 brief, guard 7)."""
    return [pid for pid in plugin_ids if f"stock:{pid}" not in plugins_list_output]


def verify_bundled_plugins(docker: DockerCLI, setup: ProbeSetup,
                           progress: Callable[[str], None] | None = None) -> None:
    """Assert every baked plugin is visible as a BUNDLED (stock) extension.

    Runs `openclaw plugins list` inside the probe container as the agent
    identity. Raises ProbeError when a baked id is missing from the stock
    source root."""
    plugin_ids = list(setup.flavour.baked_plugin_ids)
    if not plugin_ids:
        return
    try:
        result = docker.run([
            "exec",
            "-u", f"{setup.agent_uid}:{setup.agent_primary_gid}",
            "-e", f"OPENCLAW_STATE_DIR={EXEC_STATE_DIR}",
            setup.container_name,
            "openclaw", "plugins", "list",
        ])
    except DockerError as e:
        raise ProbeError(
            f"bundled-plugin check failed to run `plugins list` in the probe "
            f"container:\n{e.stderr.strip() or e}",
            exit_codes.PROBE_FAILED,
        ) from e
    missing = missing_bundled_plugins(result.stdout, plugin_ids)
    if missing:
        raise ProbeError(
            f"baked plugin(s) not visible under the stock source root: "
            f"{', '.join(missing)}. The upstream's dist/extensions layout has "
            f"likely changed (it is not a published interface) — inspect "
            f"`plugins list` in the image and adjust the wrapper bake step.",
            exit_codes.PROBE_FAILED,
        )
    if progress:
        progress(f"bundled plugins verified under stock root: {', '.join(plugin_ids)}")


def capture_diff(docker: DockerCLI, setup: ProbeSetup) -> str:
    """Snapshot `docker diff` output for the probe container."""
    return docker.diff(setup.container_name)


def stop_probe_container(docker: DockerCLI, setup: ProbeSetup) -> str:
    """Stop the probe container; return its accumulated logs."""
    logs = docker.logs(setup.container_name)
    docker.stop(setup.container_name, timeout_seconds=8)
    docker.rm_container(setup.container_name, force=True)
    return logs


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _stat_size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _walk_surface(surface_root: Path, surface_name: str) -> list[SurfaceWriteRecord]:
    """Enumerate files and directories under a probe-side surface dir.

    `docker diff` cannot observe writes through bind mounts (they pass through
    to the host), so the surface_writes section of the probe report is built
    by walking the host-side directory tree after the probe completes. Empty
    pre-created scaffolding directories (`main/`) are skipped to keep the
    report focused on actual openclaw output.
    """
    records: list[SurfaceWriteRecord] = []
    if not surface_root.is_dir():
        return records
    for path in sorted(surface_root.rglob("*")):
        try:
            rel = path.relative_to(surface_root.parent).as_posix()
        except ValueError:
            continue
        # Skip the pre-created scaffolding root itself (e.g. "configs/main")
        # when it has no files inside it.
        if path.is_dir() and not any(path.iterdir()):
            continue
        if path.is_dir():
            records.append(SurfaceWriteRecord(path=rel, change="added", kind="directory"))
        else:
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            records.append(SurfaceWriteRecord(path=rel, change="added", size_bytes=size))
    return records


def assemble_report(setup: ProbeSetup, *, ran_at: datetime, duration: float,
                    healthz: HealthCheck, readyz: HealthCheck | None,
                    diff_output: str) -> ProbeReport:
    # surface_writes comes from walking the host-side bind-mount dirs since
    # docker diff misses bind-mount writes.
    surface_writes: dict[str, list[SurfaceWriteRecord]] = {}
    for surface_name, dir_path in (
        ("configs", setup.configs_dir),
        ("memory", setup.memory_dir),
        ("sessions", setup.sessions_dir),
        ("scratch", setup.scratch_dir),
    ):
        surface_writes[surface_name] = _walk_surface(dir_path, surface_name)

    # in_container_writes comes from docker diff (which sees the container's
    # own ephemeral filesystem changes outside the bind mounts).
    entries = parse_docker_diff(diff_output)
    _, in_container = partition_diff_by_surface(entries, mount_root="/agent")

    in_container_records = [
        InContainerWriteRecord(
            path=e.path,
            change=e.change,
            size_bytes=None,                                # would require docker exec stat per file
            candidate_for_relocation=classify_relocation(e.path),
        )
        for e in in_container
    ]

    result: ProbeResult = "success" if healthz.status == 200 else "failed_healthz"

    return ProbeReport(
        image_tag=setup.image_tag,
        flavour=setup.flavour.name,
        image_version=setup.image_version,
        ran_at=ran_at,
        duration_seconds=duration,
        result=result,
        healthz=healthz,
        readyz=readyz,
        surface_writes=surface_writes,
        in_container_writes=in_container_records,
        observed_paths={},          # populated by a later pass against in-container paths
        flags_observed=[],
        notes="",
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

@dataclass
class ProbeOutcome:
    report: ProbeReport
    container_logs: str
    captured_openclaw_json: dict | None         # post-boot openclaw.json from configs/main/
    diff_output: str


def run_probe(flavour: FlavourConfig, image_tag: str, image_version: str,
              *, templates_root: Path, probe_dir_root: Path,
              docker: DockerCLI | None = None,
              progress_callback: Callable[[str], None] | None = None,
              ) -> ProbeOutcome:
    """Run the full probe loop and return its outcome.

    Raises ProbeError on container-start failure or on a healthz failure that
    cannot produce a useful report. Returns a ProbeOutcome (possibly with
    result=failed_healthz) when the container started but the gateway didn't
    answer — the caller may still want the partial probe report.
    """
    import json

    docker = docker or DockerCLI()
    progress = progress_callback or (lambda _msg: None)

    setup = ProbeSetup(
        flavour=flavour,
        image_tag=image_tag,
        image_version=image_version,
        probe_dir=probe_dir_root / f"probe-{image_version}-{uuid.uuid4().hex[:8]}",
        container_name=f"image-compile-probe-{uuid.uuid4().hex[:8]}",
        published_port=_find_free_port(),
        agent_uid=os.getuid() if hasattr(os, "getuid") else 1000,
        agent_primary_gid=os.getgid() if hasattr(os, "getgid") else 1000,
        templates_root=templates_root,
    )

    try:
        setup.probe_dir.mkdir(parents=True, exist_ok=True)
        setup_probe_dirs(setup)
        render_stub_files(setup)
    except OSError as e:
        raise ProbeError(
            f"cannot prepare probe directory {setup.probe_dir}: {e}.\n"
            f"  If {probe_dir_root} is owned by another user, either clear it "
            f"or set a per-user `probe_dir_root` in config.yml.",
            exit_codes.PROBE_FAILED,
        ) from e
    progress(f"probe dir: {setup.probe_dir}")
    progress(f"stub config + secrets rendered into {setup.configs_dir/'main'}")

    progress(f"starting probe container {setup.container_name}")
    started_at = time.monotonic()
    ran_at = datetime.now(timezone.utc)
    start_probe_container(docker, setup)

    try:
        healthz = wait_for_health(
            setup,
            timeout_seconds=flavour.probe.startup_timeout_seconds,
            progress=progress,
        )
        progress(f"healthz: status={healthz.status}")

        # Settle so any post-start writes have a chance to land.
        time.sleep(flavour.probe.settle_seconds)

        readyz = check_readyz(setup)
        progress(f"readyz: status={readyz.status}")

        verify_bundled_plugins(docker, setup, progress=progress)

        diff_output = capture_diff(docker, setup)
        progress(f"docker diff: {diff_output.count(chr(10))} change lines")

        # Capture post-boot openclaw.json
        post_config_path = setup.configs_dir / "main" / "openclaw.json"
        captured_json: dict | None = None
        try:
            captured_json = json.loads(post_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            progress(f"warning: could not re-read openclaw.json after probe: {e}")

    finally:
        container_logs = stop_probe_container(docker, setup)

    # r8 acceptance: bundled loading must not double-discover anything.
    if setup.flavour.baked_plugin_ids and DUPLICATE_PLUGIN_MARKER in container_logs:
        raise ProbeError(
            f"container logs contain {DUPLICATE_PLUGIN_MARKER!r} — a baked "
            f"plugin is being discovered twice (stale plugins.load.paths or "
            f"plugins.installs entry in the stub/surface config?)",
            exit_codes.PROBE_FAILED,
        )

    # Guard 8 (r8.1): a channel-enabled boot exercises channel start; any
    # plugin LOAD failure in the logs fails the build.
    load_errors = find_plugin_load_errors(container_logs)
    if load_errors:
        detail = "\n".join(f"  {line}" for line in load_errors[:5])
        raise ProbeError(
            f"plugin load error(s) in container logs (channel-start check):\n"
            f"{detail}\n"
            f"A baked plugin's manifest specifiers do not resolve in the "
            f"image — inspect the bake normalisation step.",
            exit_codes.PROBE_FAILED,
        )

    duration = time.monotonic() - started_at
    report = assemble_report(
        setup, ran_at=ran_at, duration=duration,
        healthz=healthz, readyz=readyz, diff_output=diff_output,
    )

    return ProbeOutcome(
        report=report,
        container_logs=container_logs,
        captured_openclaw_json=captured_json,
        diff_output=diff_output,
    )
