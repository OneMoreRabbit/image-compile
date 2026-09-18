# image-compile

Builds pinned wrapper Docker images for agent-platform flavours (openclaw, nanoclaw, ...) and
produces paired defaults bundles in the platform registry.

## Design

Design docs live in the **Atlas-AgentEco** vault, not in this repo (constitution principle 3:
one home each). This seat syncs the vault to `.atlas/`; the paths below are relative to the
vault root.

- `components/agent-image/docs/image-compile-architecture-v0_2.md` — architecture.
- `components/agent-image/docs/image-compile-development-plan-v0_1.md` — phased delivery plan.
- `components/agent-image/docs/manual/image-compile-user-manual-v0_2.md` — operator manual.
- `components/agent-image/docs/provides/` — the contracts this component publishes.

The previous links here pointed at `../integrations/` and `../docs/`, sibling directories that
have not existed since the pre-vault migration. Run `sh scripts/atlas-context.sh` for the
session briefing; see `AGENTS.md`.

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

**Released: `v0.7.0`** (2026-09-18) — the full pipeline is implemented. `build` runs preflight,
buildx, smoke, probe, bundle and push; `probe`, `list`, `verify` and `diff` are live. Two
flavours are configured (`openclaw`, `nanoclaw`). 125 tests pass.

Current wrapper baseline: `openclaw-runtime` **v0.8.0**, wrapper revision **r8.1**, against
upstream OpenClaw `2026.6.11`. Note the two numbering schemes are independent — see that repo's
CHANGELOG.

Build integrity, added after the r3 mislabelled-image incident: `--wrapper-rev` is **required**
(it sets the image tag and the wrapper-rev label, so it is declared, never defaulted) and is
cross-checked against the wrapper CHANGELOG's top `## r<N>` heading, with the wrapper worktree
required clean. `--wrapper-commit <sha>` additionally pins the exact checkout — the only check
that compares against a value recorded outside the tree being built. All fail closed — an
undeterminable answer is a refusal, not a pass. `--allow-dirty` accepts an unreproducible build
knowingly.

Probe guards: baked plugins must resolve as bundled stock extensions, no plugin-load errors, and
(guard 9/9b) the agent process's supplementary group set must **equal** the requested
`AGENT_SUPP_GIDS` and satisfy a real read through a group grant.

Images publish to `ghcr.io/onemorerabbit/` — the organisation, never a personal account.

## Tests

```bash
.venv/bin/pytest
```

Unit tests do not require Docker. The integration path (build, probe, push) is verified manually
on a Linux build host — see the development plan §Dev/test environment.
