"""Attachment lifecycle operations with revision and access checks."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from contextlib import ExitStack
from pathlib import PurePosixPath
from typing import TypedDict
from urllib.parse import quote, unquote, urlsplit

from obsidian_mcp.config import get_config
from obsidian_mcp.domain.models import (
    PreconditionRequiredError,
    RevisionConflictError,
    normalize_revision_token,
)
from obsidian_mcp.storage.filesystem import VaultStorage
from obsidian_mcp.storage.locking import acquire_lock
from obsidian_mcp.tools.attachments import validate_attachment_path

# Unsupported references are rejected rather than silently left broken.
_TEXT_FORMATS = {".md", ".canvas", ".base", ".json", ".yaml", ".yml", ".html", ".txt", ".css"}
_LINK = re.compile(
    r"\[\[(?P<wiki>[^\]\n]+)\]\]"
    r"|!?\[[^\]\n]*\]\(\s*(?P<md><[^>\n]+>|[^\s()]+)"
    r'(?:\s+["\'][^\n]*?["\'])?\s*\)'
    r"|(?m:^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(?P<ref><[^>\n]+>|[^\s]+))"
)


def _resolve(raw: str, note: str, files: set[str], wiki: bool) -> str | None:
    decoded = unquote(raw).replace("\\", "/")
    if not decoded or urlsplit(decoded).scheme or decoded.startswith("//"):
        return None
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(note), decoded))
    rooted = posixpath.normpath(decoded.lstrip("/"))
    if not wiki:
        return rooted if decoded.startswith("/") else relative
    candidates = {p for p in files if p in (rooted, relative) or p.endswith("/" + rooted)}
    if len(candidates) > 1:
        raise ValueError(
            f"Ambiguous attachment link {raw!r} in {note!r}; use a full vault path first"
        )
    return next(iter(candidates), None)


def rewrite_links(text: str, note: str, source: str, destination: str, files: set[str]):
    """Rewrite full/short wikilinks, inline Markdown, and reference definitions.

    Leaves fragments, aliases, sizes and Markdown titles intact. Unknown syntax
    mentioning the source filename fails closed (including code, YAML and HTML).
    """
    return _rewrite_many(text, note, {source: destination}, files)


def _rewrite_many(text: str, note: str, moves: dict[str, str], files: set[str]):
    """Resolve links against the original vault, never against earlier replacements."""
    filenames = {PurePosixPath(source).name for source in moves}
    changed = 0
    uncovered = []
    last = 0
    output = []
    for match in _LINK.finditer(text):
        uncovered.append(text[last : match.start()])
        group = (
            "wiki"
            if match.group("wiki") is not None
            else ("md" if match.group("md") is not None else "ref")
        )
        raw = match.group(group)
        wiki = group == "wiki"
        brackets = not wiki and raw.startswith("<") and raw.endswith(">")
        token = raw[1:-1] if brackets else raw
        # Obsidian wikilink fragment/size/alias; Markdown query/fragment.
        split = re.search(r"[#|]" if wiki else r"[?#]", token)
        target = token[: split.start()] if split else token
        tail = token[split.start() :] if split else ""
        rendered = match.group(0)
        # Resolve only links which could refer to this filename.
        if PurePosixPath(unquote(target).replace("\\", "/")).name in filenames:
            resolved = _resolve(target, note, files, wiki)
            if resolved in moves:
                destination = moves[resolved]
                replacement = (
                    destination
                    if wiki
                    else quote(
                        posixpath.relpath(destination, posixpath.dirname(note) or "."), safe="/-._~"
                    )
                )
                replacement += tail
                if brackets:
                    replacement = "<" + replacement + ">"
                a, b = match.start(group) - match.start(), match.end(group) - match.start()
                rendered = rendered[:a] + replacement + rendered[b:]
                changed += 1
        output.extend((text[last : match.start()], rendered))
        last = match.end()
    uncovered.append(text[last:])
    output.append(text[last:])
    literal_text = unquote("".join(uncovered)).casefold()
    for filename in sorted(filenames):
        if filename.casefold() in literal_text:
            raise ValueError(
                f"Unsupported or literal reference to {filename!r} in {note!r}; edit it manually first"
            )
    return "".join(output), changed


def _attachment(storage, path: str) -> str:
    # Require both read and write access, approved extension, no hidden paths.
    path = validate_attachment_path(path, write=True)
    storage.resolve_read(path)
    storage.resolve_delete(path)
    return path


def _check_revision(storage, path, expected, *, required=True):
    current = storage.revision(path)
    if expected is None and required:
        raise PreconditionRequiredError(path)
    if expected is not None and current.token != normalize_revision_token(expected):
        raise RevisionConflictError(path, expected, current)
    return current


def _plan(storage, source, destination):
    return _plan_moves(storage, {source: destination})


def _plan_moves(storage, moves):
    """Read each candidate document once, regardless of the number of attachments."""
    files = {entry.relative for entry in storage.list_files()}
    filenames = {PurePosixPath(source).name.casefold() for source in moves}
    changes = {}
    for path in sorted(files):
        if PurePosixPath(path).suffix.lower() not in _TEXT_FORMATS:
            continue
        content, revision = storage.read_bytes_with_revision(path)
        text = content.decode("utf-8")
        decoded = unquote(text).casefold()
        if not any(filename in decoded for filename in filenames):
            continue
        if not path.lower().endswith(".md"):
            raise ValueError(f"Attachment is referenced by unsupported document format: {path!r}")
        rewritten, count = _rewrite_many(text, path, moves, files)
        if count:
            # Preflight ALL affected notes before moving anything, including protected paths.
            storage.resolve_write(path)
            changes[path] = (text, rewritten, revision.token, count)
    return changes


def inspect_attachment(path: str):
    storage = VaultStorage.from_config()
    path = validate_attachment_path(path)
    revision = storage.revision(path)
    return {"path": path, "revision": revision.token, "size_bytes": storage.stat(path).st_size}


class AttachmentMove(TypedDict):
    """One explicit source-to-destination mapping in a batch."""

    from_path: str
    to_path: str


MAX_ATTACHMENT_MOVES = 500


def _batch_paths(storage, moves):
    if not isinstance(moves, list) or not 1 <= len(moves) <= MAX_ATTACHMENT_MOVES:
        raise ValueError(f"Supply between 1 and {MAX_ATTACHMENT_MOVES} attachment moves")
    mapping = {}
    destinations = set()
    for item in moves:
        if not isinstance(item, dict) or set(item) != {"from_path", "to_path"}:
            raise ValueError("Each move must contain only from_path and to_path")
        if not all(isinstance(value, str) and value for value in item.values()):
            raise ValueError("Attachment paths must be non-empty strings")
        source = _attachment(storage, item["from_path"])
        destination = validate_attachment_path(item["to_path"], write=True)
        if source == destination:
            raise ValueError("Source and destination must differ")
        if source in mapping or destination in destinations:
            raise ValueError("Duplicate source or destination in attachment batch")
        if PurePosixPath(source).suffix.lower() != PurePosixPath(destination).suffix.lower():
            raise ValueError("Keep the attachment extension unchanged")
        if any(c in destination for c in "#|[]?\n\r"):
            raise ValueError("Destination contains characters unsafe in Obsidian links")
        if storage.exists(destination, read=False):
            raise FileExistsError(f"Destination already exists: {destination!r}")
        mapping[source] = destination
        destinations.add(destination)
    if destinations.intersection(mapping):
        raise ValueError("Attachment batches cannot contain chains or swaps")
    return dict(sorted(mapping.items()))


def _batch_snapshot(storage, mapping):
    revisions = {source: storage.revision(source).token for source in mapping}
    changes = _plan_moves(storage, mapping)
    # Bind the preview to its vault, paths, attachment bytes, and affected notes.
    payload = {
        "version": 1,
        "vault": str(storage.policy.root),
        "moves": mapping,
        "revisions": revisions,
        "changes": changes,
    }
    token = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()
    )
    return revisions, changes, token


def move_attachments(
    moves: list[AttachmentMove],
    *,
    dry_run: bool = True,
    expected_plan_revision: str | None = None,
    index=None,
):
    """Preview or commit one guarded batch; each affected note is written once.

    This is a synchronous operation with rollback on ordinary failures, not a
    crash-safe transaction against external editors or filesystem failures.
    """
    if not dry_run and not expected_plan_revision:
        raise ValueError("Preview the batch first, then supply expected_plan_revision")
    storage = VaultStorage.from_config()
    mapping = _batch_paths(storage, moves)
    revisions, changes, token = _batch_snapshot(storage, mapping)
    if expected_plan_revision is not None and expected_plan_revision != token:
        raise ValueError("Attachment batch changed since preview; run a new preview")
    result = {
        "plan_revision": token,
        "moves": [
            {"from_path": source, "to_path": destination} for source, destination in mapping.items()
        ],
        "count": len(mapping),
        "updated_links_in": list(changes),
        "links": sum(change[3] for change in changes.values()),
    }
    if dry_run:
        return {**result, "status": "dry_run"}
    with ExitStack() as locks:
        for path in sorted({*mapping, *mapping.values(), *changes}):
            locks.enter_context(acquire_lock(path, lock_path=get_config().lock_path))
        # Revalidate permissions, collisions and references after acquiring locks.
        _batch_paths(storage, moves)
        if _batch_snapshot(storage, mapping)[2] != token:
            raise ValueError("Attachment batch changed during planning; run a new preview")
        moved = []
        written = {}
        try:
            for source, destination in mapping.items():
                _check_revision(storage, source, revisions[source])
                storage.move(source, destination)
                moved.append((source, destination))
            for path, (_, rewritten, expected, _) in changes.items():
                written[path] = storage.write_text_atomic(
                    path,
                    rewritten,
                    expected_revision=expected,
                ).token
        except Exception as exc:
            rollback_errors = []
            for path, committed in reversed(list(written.items())):
                try:
                    storage.write_text_atomic(path, changes[path][0], expected_revision=committed)
                except Exception:
                    rollback_errors.append(path)
            for source, destination in reversed(moved):
                try:
                    _check_revision(storage, destination, revisions[source])
                    storage.move(destination, source)
                except Exception:
                    rollback_errors.extend((source, destination))
            if rollback_errors:
                raise RuntimeError(
                    f"Partial batch rollback; inspect these paths before retrying: {rollback_errors}"
                ) from exc
            raise RuntimeError(
                "Batch failed; moved attachments and edited notes were rolled back"
            ) from exc
        if index is not None:
            for path in changes:
                index.update(path)
    return {**result, "status": "moved"}


def move_attachment(from_path, to_path, *, expected_revision=None, dry_run=True, index=None):
    storage = VaultStorage.from_config()
    source = _attachment(storage, from_path)
    destination = validate_attachment_path(to_path, write=True)
    if source == destination:
        raise ValueError("Source and destination must differ")
    if PurePosixPath(source).suffix.lower() != PurePosixPath(destination).suffix.lower():
        raise ValueError("Keep the attachment extension unchanged")
    if any(c in destination for c in "#|[]?\n\r"):
        raise ValueError("Destination contains characters unsafe in Obsidian links")
    if storage.exists(destination, read=False):
        raise FileExistsError(f"Destination already exists: {destination!r}")
    revision = _check_revision(storage, source, expected_revision, required=not dry_run)
    changes = _plan(storage, source, destination)
    result = {
        "path": destination,
        "from": source,
        "revision": revision.token,
        "updated_links_in": list(changes),
        "links": sum(c[3] for c in changes.values()),
    }
    if dry_run:
        return {**result, "status": "dry_run"}
    with ExitStack() as locks:
        for path in sorted({source, destination, *changes}):
            locks.enter_context(acquire_lock(path, lock_path=get_config().lock_path))
        _check_revision(storage, source, revision.token)
        if changes != _plan(storage, source, destination):
            raise RuntimeError("References changed during planning; run a new preview")
        # Same filesystem is required; an EXDEV failure happens before note edits.
        storage.move(source, destination)
        written = {}
        try:
            for path, (_, rewritten, expected, _) in changes.items():
                written[path] = storage.write_text_atomic(
                    path, rewritten, expected_revision=expected
                ).token
        except Exception as exc:
            rollback_errors = []
            for path, committed in reversed(list(written.items())):
                try:
                    storage.write_text_atomic(path, changes[path][0], expected_revision=committed)
                except Exception:
                    rollback_errors.append(path)
            try:
                _check_revision(storage, destination, revision.token)
                storage.move(destination, source)
            except Exception:
                rollback_errors.append(destination)
            if rollback_errors:
                raise RuntimeError(
                    f"Partial rollback; inspect these paths before retrying: {rollback_errors}"
                ) from exc
            raise RuntimeError("Move failed; attachment and edited notes were rolled back") from exc
        if index is not None:
            for path in changes:
                index.update(path)
    return {**result, "status": "moved"}


def delete_attachment(
    path, *, expected_revision=None, trash=True, dry_run=True, allow_broken_links=False
):
    storage = VaultStorage.from_config()
    path = _attachment(storage, path)
    storage.resolve_delete(path, permanent=not trash)
    revision = _check_revision(storage, path, expected_revision, required=not dry_run)
    references = _plan(storage, path, path)
    result = {
        "path": path,
        "revision": revision.token,
        "trash": trash,
        "referenced_by": list(references),
    }
    if dry_run:
        return {**result, "status": "dry_run"}
    if references and not allow_broken_links:
        raise ValueError(
            f"Attachment is still referenced by {list(references)}; remove those links first"
        )
    with ExitStack() as locks:
        for item in sorted({path, *references}):
            locks.enter_context(acquire_lock(item, lock_path=get_config().lock_path))
        _check_revision(storage, path, revision.token)
        if references != _plan(storage, path, path):
            raise RuntimeError("References changed during planning; run a new preview")
        if trash:
            _, trashed = storage.trash(path)
            result["trashed_name"] = trashed.name
        else:
            storage.delete(path)
    result.pop("revision")
    return {**result, "status": "deleted"}


def restore_attachment(trashed_name, to_path):
    storage = VaultStorage.from_config()
    destination = validate_attachment_path(to_path, write=True)
    if "/" in trashed_name or "\\" in trashed_name or trashed_name.startswith("."):
        raise ValueError("Use the bare trashed_name returned by delete_attachment_tool")
    if PurePosixPath(trashed_name).suffix.lower() != PurePosixPath(destination).suffix.lower():
        raise ValueError("Keep the attachment extension unchanged")
    with ExitStack() as locks:
        for path in sorted({".trash/" + trashed_name, destination}):
            locks.enter_context(acquire_lock(path, lock_path=get_config().lock_path))
        if storage.trash_info(trashed_name).is_dir:
            raise ValueError("Use restore_tool for folders")
        storage.restore(trashed_name, destination)
    return {
        "path": destination,
        "status": "restored",
        "revision": storage.revision(destination).token,
    }
