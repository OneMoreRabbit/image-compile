# image-compile

Builds pinned wrapper Docker images for agent-platform flavours (openclaw, nanoclaw, ...) and
produces paired defaults bundles in the platform registry.

## Design

- [image-compile-build-brief-v0_1.md](../integrations/image-compile-build-brief-v0_1.md) — parent brief.
- [image-compile-brief-amendments-v0_1.md](../docs/image-compile-brief-amendments-v0_1.md) — agreed amendments (multi-flavour, exit codes, probe corrections, image cleanup).
- [image-compile-development-plan-v0_1.md](../docs/image-compile-development-plan-v0_1.md) — phased delivery plan.

## Install (development)

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

## Usage

```
image-compile build <flavour> <upstream_version> [options]
image-compile probe <flavour> <image_tag> [options]
image-compile list [<flavour>]
image-compile verify <flavour> <image_version>
image-compile diff <flavour> <image_version_a> <image_version_b>
```

Example:

```bash
image-compile build openclaw v2026.5.5
```

See `image-compile --help` and `image-compile <verb> --help` for full options.

## Configuration

The tool reads `config.yml` next to its install location, or wherever `--config <path>` points.
Start from `config.yml.example`:

```bash
cp config.yml.example config.yml
```

Edit the `flavours:` block to add or remove flavours.

## Status

**Phase 0 — scaffolding.** All five verbs are stubbed. No build or probe logic implemented yet.
See the development plan above for what each phase delivers.

## Tests

```bash
.venv/bin/pytest
```

Unit tests do not require Docker. The integration path (build, probe, push) is verified manually
on a Linux build host — see the development plan §Dev/test environment.
