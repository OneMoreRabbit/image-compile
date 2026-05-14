"""Compatibility matrix reader/writer.

Edits ``~/registry/compatibility_matrix.yml``. The matrix is a hand-edited file
that two tools share (image-compile and agent-compile). We use ``ruamel.yaml``
round-trip mode so existing comments, key order, and quoting style survive
edits — neither tool should churn the other's writes.

Entry shape (per docs/image-compile-brief-amendments-v0_1.md §Amendment 1):

    entries:
      - flavour: openclaw
        image: 2026.5.5-r1
        template: image_defaults:openclaw:2026.5.5-r1     # or marketing_arc:v3
        status: blessed                                    # | experimental | broken
        tested_at: 2026-05-14T10:00:00Z
        test_agent: ""                                     # "" for self-blessed
        notes: ""

Image-compile only writes the self-blessed image_defaults entry. Named-template
entries are written by agent-compile.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


MatrixStatus = Literal["blessed", "experimental", "broken"]


# ---------------------------------------------------------------------------
# Entry value object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MatrixEntry:
    flavour: str
    image: str
    template: str
    status: MatrixStatus
    tested_at: datetime
    test_agent: str = ""
    notes: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        """Uniqueness key — entries with the same (flavour, image, template) are upserts."""
        return (self.flavour, self.image, self.template)

    def to_commented_map(self) -> CommentedMap:
        """Render this entry as a ruamel CommentedMap with the fields in the
        canonical order. Used when appending a brand-new entry."""
        cm = CommentedMap()
        cm["flavour"] = self.flavour
        cm["image"] = self.image
        cm["template"] = self.template
        cm["status"] = self.status
        cm["tested_at"] = self.tested_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cm["test_agent"] = self.test_agent
        cm["notes"] = self.notes
        return cm


# ---------------------------------------------------------------------------
# Self-blessed entry factory
# ---------------------------------------------------------------------------

def self_blessed_entry(flavour: str, image_version: str, *,
                       tested_at: datetime | None = None,
                       notes: str | None = None) -> MatrixEntry:
    """Build the auto-blessed `image_defaults` entry written by `image-compile build`.

    Per the agent-platform architecture v0.2: an image_defaults bundle implicitly
    counts as `blessed` against its own image. This factory produces that entry
    in the canonical form (template = `image_defaults:<flavour>:<image_version>`,
    test_agent = empty).
    """
    return MatrixEntry(
        flavour=flavour,
        image=image_version,
        template=f"image_defaults:{flavour}:{image_version}",
        status="blessed",
        tested_at=tested_at or datetime.now(timezone.utc),
        test_agent="",
        notes=notes if notes is not None else "Auto-blessed: image defaults against own image.",
    )


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------

def _yaml() -> YAML:
    """Configure a ruamel YAML instance for round-trip-safe reads and writes.

    Keep settings minimal and stable. Anything we tune here is observable to the
    other tool sharing this file (agent-compile).
    """
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096                              # don't reflow our string fields
    return y


def load_matrix(path: Path) -> CommentedMap:
    """Load the matrix file. Returns a CommentedMap containing an `entries` list,
    creating the structure if the file doesn't exist or is empty."""
    if not path.is_file():
        return _empty_matrix()
    yaml = _yaml()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        return _empty_matrix()
    if not isinstance(data, CommentedMap):
        raise ValueError(f"{path}: top-level must be a mapping (got {type(data).__name__})")
    if "entries" not in data:
        data["entries"] = CommentedSeq()
    elif not isinstance(data["entries"], (list, CommentedSeq)):
        raise ValueError(f"{path}: `entries` must be a list (got {type(data['entries']).__name__})")
    return data


def _empty_matrix() -> CommentedMap:
    cm = CommentedMap()
    cm["entries"] = CommentedSeq()
    return cm


def write_matrix(path: Path, matrix: CommentedMap) -> None:
    """Atomically write the matrix back to disk.

    Writes to a sibling temp file in the same directory (so rename is atomic
    on the same filesystem), fsyncs, then ``os.replace`` over the target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = _yaml()
    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.dump(matrix, fh)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_entry(matrix: CommentedMap, entry: MatrixEntry) -> bool:
    """Insert ``entry`` into the matrix, replacing any existing entry with the
    same (flavour, image, template) key. Returns True if an existing entry was
    replaced, False if this was a brand-new addition.

    Replacement is in-place: only the values that need changing are updated, so
    any comments anchored to the entry's position survive. Append goes to the end.
    """
    entries = matrix["entries"]
    for i, existing in enumerate(entries):
        if not isinstance(existing, (dict, CommentedMap)):
            continue
        ek = (existing.get("flavour"), existing.get("image"), existing.get("template"))
        if ek == entry.key:
            _update_in_place(existing, entry)
            return True
    entries.append(entry.to_commented_map())
    return False


def _update_in_place(target: CommentedMap, entry: MatrixEntry) -> None:
    """Apply entry's fields onto an existing ruamel CommentedMap, preserving
    keys that aren't part of MatrixEntry (e.g. operator-added extras)."""
    target["flavour"] = entry.flavour
    target["image"] = entry.image
    target["template"] = entry.template
    target["status"] = entry.status
    target["tested_at"] = entry.tested_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    target["test_agent"] = entry.test_agent
    target["notes"] = entry.notes


# ---------------------------------------------------------------------------
# Iteration helper (for verify / list verbs in later phases)
# ---------------------------------------------------------------------------

def iter_entries(matrix: CommentedMap) -> Iterator[dict]:
    """Yield each entry as a plain dict view. Useful for downstream code that
    just wants to read; mutating these does not affect the underlying matrix."""
    for entry in matrix.get("entries", []):
        if isinstance(entry, (dict, CommentedMap)):
            yield dict(entry)
