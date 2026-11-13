---
title: Release notes — image-compile
status: active
updated: 2026-11-13
owner: agent-eco.component.agent-image
about: one short entry per release, newest first — what changed and what anyone must do about it
---

# Release notes — image-compile

Newest first. One entry per release tag, written in the same commit that cuts the
tag. Plain English: a reader skimming this page should know what changed and
whether they have to act. Detail lives in commits, contracts and troubleshooting
reports — link them, don't repeat them.

## v0.7.1 — 2026-11-13

**TL;DR:** hardens probe guard 9 and adds the compiler-import guard; the release the r10.2 build runs on.

- Guard 9 no longer passes vacuously when the supplementary-group request is
  empty — "the set equals the request" now has no empty-equals-empty escape
  (`f228959`, held through the build-nothing hold).
- New test: `image_compile` imports no other compiler (constitution
  principle 2, estate need `agenteco-needs-compiler-import-guard-v0_1`).
  Source-read with `ast`; proven able to fail.
- First release carrying this release-notes file (method 1.34.0 rule).

**Action:** none for consumers — no contract moves. The estate builds r10.2
with this tag.
