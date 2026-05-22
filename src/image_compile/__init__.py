"""image-compile — build pinned wrapper images for agent-platform flavours."""

# Single source of truth for the tool version. Bump on every release-worthy
# change. Scheme: 0.<phase>.<patch> — minor = highest completed phase from the
# development plan, patch = fixes since. pyproject.toml reads this via
# [tool.hatch.version]; `image-compile --version` and each bundle's
# metadata.yml (`image_compile_version`) both read it at runtime.
__version__ = "0.3.0"
