"""Write config changes back to YAML without destroying the file.

The settings form has to save into a file a human wrote and will keep reading.
``yaml.safe_dump`` would round-trip the *data* fine and wreck everything else:
comments gone, key order reshuffled, ``description: >`` block scalars reflowed
into one long quoted line, ``marketplaces: [facebook]`` expanded to a block
list. After one form save the Advanced editor would show a file its owner
didn't recognise.

So this module never dumps the document. It **diffs** the submitted values
against what's on disk, applies only the differing keys to a round-trip parse,
and lets ruamel re-emit everything it didn't touch byte-for-byte. Untouched
keys produce no operations, so their comments and formatting survive because
they are literally never rewritten.

The only module that imports ruamel; its public surface is ``str``/``dict`` so
no ruamel types leak into the strictly-typed rest of the codebase.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError

__all__ = [
    "ConfigWriteConflict",
    "Operation",
    "apply_operations",
    "diff_config",
    "etag_for",
    "patch_yaml",
    "write_config",
]

# Lists matched by identity rather than position, so reordering or deleting a
# middle entry doesn't rewrite every following one (and take its comments).
# Maps a top-level list key to the fields that identify an entry.
_LIST_IDENTITY: dict[str, tuple[str, ...]] = {
    # An item's name is its key in the seen store, so it's a genuine identity.
    "items": ("name",),
    # Deliberately NOT the topic: that's the field people edit, and using it as
    # the identity would make every topic change look like a different notifier
    # and rewrite the whole list. Two of the same type fall back to positional.
    "notifiers": ("type",),
}

# A mapping value written as a literal `null` rather than left empty.
_NULL_VALUE = re.compile(r"^\s*[^#\s][^:]*:\s*null\s*$", re.MULTILINE)


class ConfigWriteConflict(ConfigError):
    """The file changed since the form loaded it."""

    def __init__(self, message: str, *, current_text: str) -> None:
        super().__init__(message, kind="conflict")
        self.current_text = current_text


@dataclass(frozen=True)
class Operation:
    """One change to make. ``path`` is a list of mapping keys / list indices."""

    op: str  # "set" | "unset"
    path: tuple[Any, ...]
    value: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"op": self.op, "path": list(self.path), "value": self.value}


def etag_for(text: str) -> str:
    """Identity of a config file's exact bytes, for optimistic concurrency."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identity_of(key: str, entry: Any) -> tuple[Any, ...] | None:
    """A stable identity for a list entry, or None if we can't derive one."""
    fields = _LIST_IDENTITY.get(key)
    if not fields or not isinstance(entry, dict):
        return None
    identity = tuple(entry.get(f) for f in fields if f in entry)
    return identity or None


def _match_list(key: str, old: list[Any], new: list[Any]) -> list[tuple[int | None, int]]:
    """Pair up (old_index, new_index) for a list, by identity where possible.

    ``old_index`` is None for an entry that has no counterpart (an insert).
    """
    old_ids = [_identity_of(key, e) for e in old]
    # Duplicate identities make matching ambiguous; fall back to positional.
    usable = [i for i in old_ids if i is not None]
    if not usable or len(set(usable)) != len(usable):
        return [(i if i < len(old) else None, i) for i in range(len(new))]

    lookup = {ident: idx for idx, ident in enumerate(old_ids) if ident is not None}
    pairs: list[tuple[int | None, int]] = []
    for new_index, entry in enumerate(new):
        ident = _identity_of(key, entry)
        matched = lookup.get(ident) if ident is not None else None
        if matched is None and new_index < len(old):
            # No identity match — most often a rename. Falling back to the same
            # position keeps that a one-field edit instead of rewriting the whole
            # list (and losing the comments inside it). The structural guard
            # still verifies the result, so a wrong guess can't corrupt data.
            matched = new_index
        pairs.append((matched, new_index))
    return pairs


def diff_config(old: Any, new: Any, *, path: tuple[Any, ...] = ()) -> list[Operation]:
    """Operations that turn ``old`` into ``new``, touching as little as possible."""
    if isinstance(old, dict) and isinstance(new, dict):
        ops: list[Operation] = []
        for key in old:
            if key not in new:
                ops.append(Operation("unset", (*path, key)))
        for key, value in new.items():
            if key not in old:
                ops.append(Operation("set", (*path, key), value))
            else:
                ops.extend(diff_config(old[key], value, path=(*path, key)))
        return ops

    if isinstance(old, list) and isinstance(new, list):
        return _diff_list(old, new, path=path)

    return [] if old == new and type(old) is type(new) else [Operation("set", path, new)]


def _diff_list(old: list[Any], new: list[Any], *, path: tuple[Any, ...]) -> list[Operation]:
    key = str(path[-1]) if path else ""
    # A length change means indices shift; replacing wholesale is the only
    # correct option, and reordering has no meaningful "minimal patch" anyway.
    if len(old) != len(new):
        return [Operation("set", path, new)]
    ops: list[Operation] = []
    for old_index, new_index in _match_list(key, old, new):
        if old_index is None or old_index != new_index:
            return [Operation("set", path, new)]
        ops.extend(diff_config(old[old_index], new[new_index], path=(*path, new_index)))
    return ops


def apply_operations(document: Any, operations: list[Operation]) -> Any:
    """Apply operations to a plain structure (used to verify the patched text)."""
    for operation in operations:
        if not operation.path:
            document = operation.value
            continue
        target = document
        for step in operation.path[:-1]:
            target = target[step]
        last = operation.path[-1]
        if operation.op == "unset":
            del target[last]
        else:
            target[last] = copy.deepcopy(operation.value)
    return document


def _detect_sequence_indent(text: str) -> tuple[int, int]:
    """Work out the file's block-sequence indentation: (sequence, offset).

    ruamel does *not* infer this on load, and its default (dash at the parent's
    column) reformats every list in a file written the common way::

        notifiers:
          - type: ntfy      # offset 2, sequence 4

    Getting it wrong turns a one-field edit into a whole-file diff.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            continue
        dash_indent = len(line) - len(stripped)
        # Find the mapping key this sequence belongs to.
        for previous in reversed(lines[:index]):
            bare = previous.strip()
            if not bare or bare.startswith("#"):
                continue
            parent_indent = len(previous) - len(previous.lstrip())
            offset = dash_indent - parent_indent
            if offset < 0:
                break
            return parent_indent + offset + 2, offset
        break
    return 4, 2  # the style config.example.yaml uses


def _emits_explicit_null(text: str) -> bool:
    """Does this file write ``null`` rather than leaving the value empty?

    Round-tripping loses the distinction, and both mean the same thing — but
    silently rewriting ``session_path: null`` to ``session_path:`` is a diff
    the user didn't ask for.
    """
    return bool(_NULL_VALUE.search(text))


# Long prose written in the form should come back out as a readable block in
# the file, not one enormous line (or a quoted string full of \n escapes).
_BLOCK_SCALAR_KEYS = frozenset({"description"})
_FOLD_LONGER_THAN = 80


def _styled(key: Any, value: Any) -> Any:
    """Give prose values a block-scalar style so the file stays human-editable.

    Recurses, because a whole list or block can be set in one operation — on a
    first run the entire ``items`` list arrives at once, descriptions included.
    """
    if isinstance(value, dict):
        return {k: _styled(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_styled(key, v) for v in value]
    if not isinstance(value, str):
        return value
    if "\n" in value:
        from ruamel.yaml.scalarstring import LiteralScalarString

        # Keep the author's line breaks; a folded scalar would eat them.
        return LiteralScalarString(value if value.endswith("\n") else value + "\n")
    if key in _BLOCK_SCALAR_KEYS and len(value) > _FOLD_LONGER_THAN:
        from ruamel.yaml.scalarstring import FoldedScalarString

        return FoldedScalarString(value)
    return value


def patch_yaml(text: str, operations: list[Operation]) -> str:
    """Apply operations to YAML text, preserving everything untouched."""
    from ruamel.yaml import YAML
    from ruamel.yaml.representer import RoundTripRepresenter

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    # Wide enough not to rewrap prose the user already wrapped themselves
    # (verified: an untouched round-trip of a real config changes nothing at
    # this width, and does rewrap at 80), but narrow enough that a description
    # written in the form comes out as readable wrapped lines rather than one
    # very long one.
    yaml_rt.width = 88
    sequence, offset = _detect_sequence_indent(text)
    yaml_rt.indent(mapping=2, sequence=sequence, offset=offset)
    if _emits_explicit_null(text):

        class _NullRepresenter(RoundTripRepresenter):
            pass

        _NullRepresenter.add_representer(
            type(None),
            lambda self, data: self.represent_scalar("tag:yaml.org,2002:null", "null"),
        )
        yaml_rt.Representer = _NullRepresenter

    document = yaml_rt.load(text) if text.strip() else {}
    if document is None:
        document = {}

    for operation in operations:
        if not operation.path:
            document = operation.value
            continue
        target = document
        for step in operation.path[:-1]:
            target = target[step]
        last = operation.path[-1]
        if operation.op == "unset":
            if isinstance(target, dict) and last in target:
                del target[last]
            elif isinstance(target, list):
                del target[last]
        else:
            target[last] = _styled(last, operation.value)

    stream = StringIO()
    yaml_rt.dump(document, stream)
    out = stream.getvalue()
    # Match the original's trailing-newline habit so a save isn't a diff by itself.
    if text and not text.endswith("\n"):
        out = out.rstrip("\n")
    return out


def build_patched_text(current_text: str, submitted: dict[str, Any]) -> tuple[str, list[Operation]]:
    """Produce the new file contents for ``submitted``, with a correctness guard.

    Raises :class:`ConfigError` if the patched text doesn't parse back to
    exactly what the operations say it should. That check is what makes "a form
    save can never silently corrupt or drop config the form doesn't model" a
    machine-verified property rather than a promise.
    """
    original = yaml.safe_load(current_text) if current_text.strip() else {}
    if original is None:
        original = {}
    if not isinstance(original, dict):
        raise ConfigError("the settings file's top level must be a mapping", kind="yaml")

    operations = diff_config(original, submitted)
    if not operations:
        return current_text, []

    patched = patch_yaml(current_text, operations)

    expected = apply_operations(copy.deepcopy(original), operations)
    actual = yaml.safe_load(patched) if patched.strip() else {}
    if actual != expected:
        raise ConfigError(
            "Refusing to save: rewriting the settings file would have changed "
            "something other than what you edited. Your file has not been touched. "
            "You can edit it directly under Advanced.",
            kind="internal",
        )
    return patched, operations


def write_config(
    path: str | Path,
    submitted: dict[str, Any],
    *,
    etag: str | None,
    validate: Any = None,
) -> dict[str, Any]:
    """Save form values into ``path``, preserving comments and formatting.

    ``etag`` is the identity of the text the form loaded: pass None to mean "I
    expect no file here". A mismatch raises :class:`ConfigWriteConflict` rather
    than overwriting an edit made elsewhere.

    ``validate`` is called with the produced text before anything is written,
    so an invalid result can never reach disk.
    """
    p = Path(path)
    current_text = p.read_text(encoding="utf-8") if p.is_file() else ""
    current_etag = etag_for(current_text) if p.is_file() else None

    if etag != current_etag:
        raise ConfigWriteConflict(
            "Your settings file changed since this page opened — maybe you edited it "
            "under Advanced, or in a text editor. Reload to see the current settings.",
            current_text=current_text,
        )

    patched, operations = build_patched_text(current_text, submitted)
    if validate is not None:
        validate(patched)
    if operations:
        _atomic_write(p, patched)
    return {
        "text": patched,
        "etag": etag_for(patched),
        "changed": [".".join(str(s) for s in op.path) for op in operations],
    }


def _atomic_write(path: Path, text: str) -> None:
    """Replace the file in one step, keeping one .bak generation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", delete=False
    )
    try:
        with tmp:
            tmp.write(text)
        os.replace(tmp.name, path)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise
