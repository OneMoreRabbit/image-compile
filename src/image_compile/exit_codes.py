"""Tool-level exit codes.

Tool-level codes use the 20-29 range so the wrapper container's own 3-7 codes
pass through unambiguously. See docs/image-compile-brief-amendments-v0_1.md
Amendment 2.
"""

OK = 0

TAG_OR_BUNDLE_EXISTS = 20
UPSTREAM_NOT_FOUND = 21
DOCKER_BUILD_FAILED = 22
SMOKE_FAILED = 23
PROBE_FAILED = 24
PUSH_FAILED = 25
REGISTRY_WRITE_FAILED = 26
PREFLIGHT_FAILED = 27
CONFIG_ERROR = 28
INTERNAL_ERROR = 29
