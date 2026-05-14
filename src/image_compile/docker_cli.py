"""Thin wrapper around the docker CLI.

Reasons we shell out instead of using the Python Docker SDK:
- Simpler debugging (the command we ran is the command an operator can re-run).
- No coupling to docker-py's release cadence.
- The set of operations we need is small (run, build, push, pull, exec, cp, rm, diff, image inspect).

The DockerCLI class is the interface the rest of the tool consumes. Tests
substitute a fake by passing in a runner callable.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DockerError(RuntimeError):
    """Raised when a docker invocation exits non-zero."""

    def __init__(self, argv: list[str], returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(
            f"docker {' '.join(argv)!r} exited {returncode}\nstderr:\n{stderr}"
        )
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class DockerMissingError(RuntimeError):
    """Raised when the docker binary isn't on PATH."""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DockerResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


# ---------------------------------------------------------------------------
# Runner protocol (injectable for tests)
# ---------------------------------------------------------------------------

class Runner(Protocol):
    def __call__(self, argv: list[str], *, check: bool, capture_output: bool,
                 timeout: float | None) -> DockerResult: ...


def _real_runner(argv: list[str], *, check: bool, capture_output: bool,
                 timeout: float | None) -> DockerResult:
    """Default runner: shells out via subprocess."""
    if shutil.which(argv[0]) is None:
        raise DockerMissingError(f"{argv[0]!r} not found on PATH")
    try:
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise DockerError(argv, returncode=-1, stdout=e.stdout or "", stderr=str(e)) from e
    result = DockerResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
    if check and proc.returncode != 0:
        raise DockerError(argv, proc.returncode, result.stdout, result.stderr)
    return result


# ---------------------------------------------------------------------------
# DockerCLI
# ---------------------------------------------------------------------------

class DockerCLI:
    """Thin facade over `docker` for the subset of operations image-compile needs."""

    def __init__(self, binary: str = "docker", runner: Runner | None = None,
                 verbose_log: Callable[[str], None] | None = None) -> None:
        self.binary = binary
        self._runner = runner or _real_runner
        self._verbose_log = verbose_log or (lambda _msg: None)

    # ---- generic ----------------------------------------------------------

    def run(self, args: Iterable[str], *, check: bool = True, capture_output: bool = True,
            timeout: float | None = None) -> DockerResult:
        argv = [self.binary, *args]
        self._verbose_log("$ " + " ".join(argv))
        return self._runner(argv, check=check, capture_output=capture_output, timeout=timeout)

    # ---- targeted helpers (filled in as Phase 1+ needs them) -------------

    def image_exists_locally(self, tag: str) -> bool:
        result = self.run(["image", "inspect", tag], check=False)
        return result.returncode == 0

    def rm_image(self, tag: str, *, force: bool = False) -> None:
        args = ["rmi"]
        if force:
            args.append("--force")
        args.append(tag)
        self.run(args, check=False)

    def diff(self, container: str) -> str:
        return self.run(["diff", container]).stdout

    def stop(self, container: str, *, timeout_seconds: int = 10) -> None:
        self.run(["stop", "-t", str(timeout_seconds), container], check=False)

    def rm_container(self, container: str, *, force: bool = False) -> None:
        args = ["rm"]
        if force:
            args.append("--force")
        args.append(container)
        self.run(args, check=False)

    def logs(self, container: str) -> str:
        # Capture both streams. `docker logs` writes container stderr to its own stderr;
        # for our purposes we want both concatenated into a single text.
        result = self.run(["logs", container], check=False)
        return (result.stdout or "") + (result.stderr or "")
