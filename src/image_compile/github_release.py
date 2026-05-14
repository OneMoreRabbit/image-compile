"""Minimal GitHub API client for upstream-release existence checks.

We don't need a full SDK. One GET against /repos/{owner}/{repo}/releases/tags/{tag}
is the entire surface area. Unauthenticated calls work for public repos within
the IP-based rate limit (60/hr), which is plenty given how rarely we build.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests


GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 10


class UpstreamReleaseError(Exception):
    """Raised when a release lookup fails for any non-404 reason."""


@dataclass(frozen=True)
class ReleaseLookup:
    repo: str
    tag: str
    exists: bool
    status_code: int
    rate_remaining: int | None = None


def check_release(repo: str, tag: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                  session: requests.Session | None = None) -> ReleaseLookup:
    """Look up a tagged release on GitHub.

    Returns a ReleaseLookup describing the outcome. 404 is *not* an exception
    (that's a normal "no such release" result); other non-200 codes raise.

    If GITHUB_TOKEN is set in the environment it's used (raises the rate limit).
    """
    url = f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "image-compile/0.1.0",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    s = session or requests
    try:
        resp = s.get(url, headers=headers, timeout=timeout_seconds)
    except requests.RequestException as e:
        raise UpstreamReleaseError(f"network error contacting GitHub: {e}") from e

    rate_remaining: int | None = None
    rr = resp.headers.get("X-RateLimit-Remaining")
    if rr is not None:
        try:
            rate_remaining = int(rr)
        except ValueError:
            rate_remaining = None

    if resp.status_code == 200:
        return ReleaseLookup(repo=repo, tag=tag, exists=True,
                             status_code=200, rate_remaining=rate_remaining)
    if resp.status_code == 404:
        return ReleaseLookup(repo=repo, tag=tag, exists=False,
                             status_code=404, rate_remaining=rate_remaining)
    if resp.status_code == 403 and rate_remaining == 0:
        raise UpstreamReleaseError(
            "GitHub API rate limit exhausted. Set GITHUB_TOKEN or use --no-validate-upstream."
        )
    raise UpstreamReleaseError(
        f"unexpected {resp.status_code} from GitHub for {repo}@{tag}: {resp.text[:200]}"
    )
