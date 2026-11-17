"""Attachment mutations for obsidian-remote-mcp 2.1.2; no shell access."""
from __future__ import annotations

import posixpath
import re
from contextlib import ExitStack
from pathlib import PurePosixPath
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
_TEXT_FORMATS = {'.md', '.canvas', '.base', '.json', '.yaml', '.yml', '.html', '.txt', '.css'}
_LINK = re.compile(
    r'\[\[(?P<wiki>[^\]\n]+)\]\]'
    r'|!?\[[^\]\n]*\]\(\s*(?P<md><[^>\n]+>|[^\s()]+)'
    r'(?:\s+["\'][^\n]*?["\'])?\s*\)'
    r'|(?m:^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(?P<ref><[^>\n]+>|[^\s]+))'
)


def _resolve(raw: str, note: str, files: set[str], wiki: bool) -> str | None:
    decoded = unquote(raw).replace('\\', '/')
    if not decoded or urlsplit(decoded).scheme or decoded.startswith('//'):
        return None
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(note), decoded))
    rooted = posixpath.normpath(decoded.lstrip('/'))
    if not wiki:
        return rooted if decoded.startswith('/') else relative
    candidates = {p for p in files if p in (rooted, relative) or p.endswith('/' + rooted)}
    if len(candidates) > 1:
        raise ValueError(f'Ambiguous attachment link {raw!r} in {note!r}; use a full vault path first')
    return next(iter(candidates), None)


def rewrite_links(text: str, note: str, source: str, destination: str, files: set[str]):
    """Rewrite full/short wikilinks, inline Markdown, and reference definitions.

    Leaves fragments, aliases, sizes and Markdown titles intact. Unknown syntax
    mentioning the source filename fails closed (including code, YAML and HTML).
    """
    filename = PurePosixPath(source).name
    changed = 0
    uncovered = []
    last = 0
    output = []
    for match in _LINK.finditer(text):
        uncovered.append(text[last:match.start()])
        group = 'wiki' if match.group('wiki') is not None else ('md' if match.group('md') is not None else 'ref')
        raw = match.group(group)
        wiki = group == 'wiki'
        brackets = not wiki and raw.startswith('<') and raw.endswith('>')
        token = raw[1:-1] if brackets else raw
        # Obsidian wikilink fragment/size/alias; Markdown query/fragment.
        split = re.search(r'[#|]' if wiki else r'[?#]', token)
        target = token[:split.start()] if split else token
        tail = token[split.start():] if split else ''
        rendered = match.group(0)
        # Resolve only links which could refer to this filename.
        if PurePosixPath(unquote(target).replace('\\', '/')).name == filename:
            resolved = _resolve(target, note, files, wiki)
            if resolved == source:
                replacement = destination if wiki else quote(
                    posixpath.relpath(destination, posixpath.dirname(note) or '.'), safe='/-._~'
                )
                replacement += tail
                if brackets:
                    replacement = '<' + replacement + '>'
                a, b = match.start(group) - match.start(), match.end(group) - match.start()
                rendered = rendered[:a] + replacement + rendered[b:]
                changed += 1
        output.extend((text[last:match.start()], rendered))
        last = match.end()
    uncovered.append(text[last:])
    output.append(text[last:])
    if filename.casefold() in unquote(''.join(uncovered)).casefold():
        raise ValueError(f'Unsupported or literal reference to {filename!r} in {note!r}; edit it manually first')
    return ''.join(output), changed


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
    files = {entry.relative for entry in storage.list_files()}
    changes = {}
    for path in sorted(files):
        if PurePosixPath(path).suffix.lower() not in _TEXT_FORMATS:
            continue
        content, revision = storage.read_bytes_with_revision(path)
        text = content.decode('utf-8')
        if PurePosixPath(source).name.casefold() not in unquote(text).casefold():
            continue
        if not path.lower().endswith('.md'):
            raise ValueError(f'Attachment is referenced by unsupported document format: {path!r}')
        rewritten, count = rewrite_links(text, path, source, destination, files)
        if count:
            # Preflight ALL affected notes before moving anything, including Human.
            storage.resolve_write(path)
            changes[path] = (text, rewritten, revision.token, count)
    return changes


def inspect_attachment(path: str):
    storage = VaultStorage.from_config()
    path = validate_attachment_path(path)
    revision = storage.revision(path)
    return {'path': path, 'revision': revision.token, 'size_bytes': storage.stat(path).st_size}


def move_attachment(from_path, to_path, *, expected_revision=None, dry_run=True, index=None):
    storage = VaultStorage.from_config()
    source = _attachment(storage, from_path)
    destination = validate_attachment_path(to_path, write=True)
    if source == destination:
        raise ValueError('Source and destination must differ')
    if PurePosixPath(source).suffix.lower() != PurePosixPath(destination).suffix.lower():
        raise ValueError('Keep the attachment extension unchanged')
    if any(c in destination for c in '#|[]?\n\r'):
        raise ValueError('Destination contains characters unsafe in Obsidian links')
    if storage.exists(destination, read=False):
        raise FileExistsError(f'Destination already exists: {destination!r}')
    revision = _check_revision(storage, source, expected_revision, required=not dry_run)
    changes = _plan(storage, source, destination)
    result = {'path': destination, 'from': source, 'revision': revision.token,
              'updated_links_in': list(changes), 'links': sum(c[3] for c in changes.values())}
    if dry_run:
        return {**result, 'status': 'dry_run'}
    with ExitStack() as locks:
        for path in sorted({source, destination, *changes}):
            locks.enter_context(acquire_lock(path, lock_path=get_config().lock_path))
        _check_revision(storage, source, revision.token)
        if changes != _plan(storage, source, destination):
            raise RuntimeError('References changed during planning; run a new preview')
        # Same filesystem is required; an EXDEV failure happens before note edits.
        storage.move(source, destination)
        written = {}
        try:
            for path, (_, rewritten, expected, _) in changes.items():
                written[path] = storage.write_text_atomic(path, rewritten, expected_revision=expected).token
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
                raise RuntimeError(f'Partial rollback; inspect these paths before retrying: {rollback_errors}') from exc
            raise RuntimeError('Move failed; attachment and edited notes were rolled back') from exc
        if index is not None:
            for path in changes:
                index.update(path)
    return {**result, 'status': 'moved'}


def delete_attachment(path, *, expected_revision=None, trash=True, dry_run=True, allow_broken_links=False):
    storage = VaultStorage.from_config()
    path = _attachment(storage, path)
    storage.resolve_delete(path, permanent=not trash)
    revision = _check_revision(storage, path, expected_revision, required=not dry_run)
    references = _plan(storage, path, path)
    result = {'path': path, 'revision': revision.token, 'trash': trash, 'referenced_by': list(references)}
    if dry_run:
        return {**result, 'status': 'dry_run'}
    if references and not allow_broken_links:
        raise ValueError(f'Attachment is still referenced by {list(references)}; remove those links first')
    with ExitStack() as locks:
        for item in sorted({path, *references}):
            locks.enter_context(acquire_lock(item, lock_path=get_config().lock_path))
        _check_revision(storage, path, revision.token)
        if references != _plan(storage, path, path):
            raise RuntimeError('References changed during planning; run a new preview')
        if trash:
            _, trashed = storage.trash(path)
            result['trashed_name'] = trashed.name
        else:
            storage.delete(path)
    result.pop('revision')
    return {**result, 'status': 'deleted'}


def restore_attachment(trashed_name, to_path):
    storage = VaultStorage.from_config()
    destination = validate_attachment_path(to_path, write=True)
    if '/' in trashed_name or '\\' in trashed_name or trashed_name.startswith('.'):
        raise ValueError('Use the bare trashed_name returned by delete_attachment_tool')
    if PurePosixPath(trashed_name).suffix.lower() != PurePosixPath(destination).suffix.lower():
        raise ValueError('Keep the attachment extension unchanged')
    with ExitStack() as locks:
        for path in sorted({'.trash/' + trashed_name, destination}):
            locks.enter_context(acquire_lock(path, lock_path=get_config().lock_path))
        if storage.trash_info(trashed_name).is_dir:
            raise ValueError('Use restore_tool for folders')
        storage.restore(trashed_name, destination)
    return {'path': destination, 'status': 'restored', 'revision': storage.revision(destination).token}


def register(server):
    """Use the original authenticated MCP instance and vault middleware."""
    from obsidian_mcp.tools.audit import log_write

    def inspect_attachment_tool(path: str, vault: str | None = None) -> dict:
        """Get an attachment revision before moving/deleting; does not return file bytes."""
        return server._read_envelope(path, inspect_attachment(path))

    @server._mutation_boundary
    def move_attachment_tool(from_path: str, to_path: str, expected_revision: str | None = None,
                             dry_run: bool = True, vault: str | None = None) -> dict:
        """Move/rename one image, PDF or other approved attachment, updating Markdown links.

        Preview first (dry_run=True), then supply its revision and dry_run=False.
        Respects WRITE_PATHS at both ends and for every rewritten note; Human
        references block the entire move. Unsupported/ambiguous reference syntax
        is rejected before mutation. Source and destination must share a filesystem.
        """
        result = move_attachment(from_path, to_path, expected_revision=expected_revision,
                                 dry_run=dry_run, index=server._index)
        if not dry_run:
            log_write('move_attachment_tool', to_path, f'moved from {from_path}')
        return server._write_envelope(result)

    @server._mutation_boundary
    def delete_attachment_tool(path: str, expected_revision: str | None = None,
                               trash: bool = True, dry_run: bool = True,
                               allow_broken_links: bool = False, vault: str | None = None) -> dict:
        """Delete one approved attachment, defaulting to the vault trash.

        Preview first, then pass its revision and dry_run=False. References block
        deletion by default; allow_broken_links=True explicitly leaves dangling
        references in writable notes. References in protected notes always block.
        Permanent deletion also requires ALLOW_PERMANENT_DELETE=true.
        """
        result = delete_attachment(path, expected_revision=expected_revision, trash=trash,
                                   dry_run=dry_run, allow_broken_links=allow_broken_links)
        if not dry_run:
            log_write('delete_attachment_tool', path, f'deleted attachment (trash={trash})')
        return server._write_envelope(result)

    def restore_attachment_tool(trashed_name: str, to_path: str, vault: str | None = None) -> dict:
        """Restore one attachment from trash into a writable path; never overwrite a file."""
        result = restore_attachment(trashed_name, to_path)
        log_write('restore_attachment_tool', to_path, f'restored attachment from {trashed_name}')
        return server._write_envelope(result)

    server.mcp.tool()(inspect_attachment_tool)
    if server._feature_flags.enable_move:
        server.mcp.tool()(move_attachment_tool)
    if server._feature_flags.enable_delete:
        server.mcp.tool()(delete_attachment_tool)
        server.mcp.tool()(restore_attachment_tool)
