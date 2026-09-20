"""Resumable, non-destructive document backups with explicit coverage."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def backup(call, destination, parent=None, max_items=200):
    """One writer per destination; never silently clear a possibly active lock."""
    root = Path(destination).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = root / '.backup.lock'
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError:
        raise RuntimeError('Backup lock exists. Wait for the active run; after a crash, confirm no run is active before removing .backup.lock.') from None
    try:
        with handle:
            handle.write(str(os.getpid()))
        return _backup(call, root, parent, max_items)
    finally:
        lock.unlink(missing_ok=True)


def _backup(call, destination, parent=None, max_items=200):
    """Mirror document archives by immutable ID; manifest retains folder metadata."""
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / 'manifest.json'
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding='utf-8'))
        if previous.get('schema') != 1:
            raise ValueError('Unsupported backup manifest; choose a different destination')
        if previous.get('parent') != parent:
            raise ValueError('Backup scope differs from existing manifest; choose a different destination')
    else:
        previous = {'schema': 1, 'parent': parent, 'documents': {}}
    docs = dict(previous.get('documents', {}))
    entries, listing_errors, seen = [], [], set()
    offset = 0
    cursor = None
    for _ in range(1000):
        # Cloud always refreshes the root; retain cached immutable metadata.
        request = {'limit': 200, 'offset': offset, 'refresh': False, 'include_tags': False}
        if cursor is not None:
            request['cursor'] = cursor
        response = call('list', request)
        if not response.get('ok'):
            listing_errors.append({'code': response.get('code', 'LIST_FAILED'), 'error': response.get('error', 'List failed')})
            break
        page = response['result']
        if page.get('walkErrors') or page.get('complete') is False:
            listing_errors.append({'code': 'PARTIAL_LIBRARY', 'error': 'Some library metadata could not be read'})
        batch = page.get('entries', [])
        repeated = False
        for entry in batch:
            if entry['id'] in seen:
                listing_errors.append({'code': 'LIBRARY_CHANGED', 'id': entry['id']})
                repeated = True
            else:
                entries.append(entry)
                seen.add(entry['id'])
        if repeated:
            break
        if not page.get('truncated') and not page.get('has_more'):
            if 'matched' in page and page['matched'] != len(seen):
                listing_errors.append({'code': 'PARTIAL_LIBRARY', 'error': 'Listing total does not match collected unique entries'})
            break
        if not batch:
            listing_errors.append({'code': 'EMPTY_PAGE'})
            break
        offset += len(batch)
        cursor = page.get('next_cursor')
    else:
        listing_errors.append({'code': 'PAGE_LIMIT'})
    selected_parents = {parent} if parent is not None else None
    if selected_parents is not None:
        for _ in range(len(entries) + 1):
            old = len(selected_parents)
            selected_parents.update(e['id'] for e in entries if e.get('type') == 'folder' and e.get('parent') in selected_parents)
            if len(selected_parents) == old:
                break
    folders = [e for e in entries if e.get('type') == 'folder'
               and (selected_parents is None or e['id'] in selected_parents)]
    selected = [e for e in entries if e.get('type') != 'folder' and not e.get('deleted') and e.get('parent') != 'trash'
                and (selected_parents is None or e.get('parent') in selected_parents)]
    results, errors = [], list(listing_errors)
    limit = max(1, min(int(max_items), 5000))
    downloaded = skipped = 0
    for entry in selected:
        entry_id = entry['id']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', entry_id):
            errors.append({'id': entry_id, 'code': 'BAD_ID'})
            continue
        target = destination / (entry_id + '.zip')
        old = docs.get(entry_id, {})
        version = entry.get('version')
        if version and version == old.get('version') and target.is_file() and target.stat().st_size == old.get('bytes'):
            skipped += 1
            results.append({'id': entry_id, 'status': 'unchanged'})
            continue
        if downloaded >= limit:
            results.append({'id': entry_id, 'status': 'pending'})
            continue
        # Never overwrite a user's unrelated file: only our manifest-owned archive.
        if target.exists() and not old:
            errors.append({'id': entry_id, 'code': 'EXISTS', 'error': 'Unmanaged target already exists'})
            continue
        # Local transports deliberately never overwrite. Stage for every
        # transport, verify bytes, then publish only a manifest-owned replacement.
        with tempfile.TemporaryDirectory(prefix='.backup-', dir=destination) as temp:
            staged = Path(temp) / (entry_id + '.zip')
            response = call('download', {'id': entry_id, 'outPath': str(staged), 'format': 'archive', 'overwrite': False})
            result = response.get('result', {})
            if not response.get('ok') or staged.is_symlink() or not staged.is_file() or staged.stat().st_size != result.get('bytes'):
                errors.append({'id': entry_id, 'code': response.get('code', 'READBACK_FAILED'), 'error': response.get('error', 'Archive download not verified')})
                continue
            if old:
                os.replace(staged, target)
            else:
                # Exclusive publication also protects a file created after our
                # earlier existence check. Do not downgrade to an unsafe copy.
                os.link(staged, target)
        docs[entry_id] = {**entry, 'bytes': target.stat().st_size, 'file': target.name}
        downloaded += 1
        results.append({'id': entry_id, 'status': 'downloaded'})
        atomic_json(manifest_path, {'schema': 1, 'parent': parent, 'documents': docs, 'folders': folders})
    removed = sorted(set(docs) - {e['id'] for e in selected}) if not listing_errors else []
    pending = sum(r['status'] == 'pending' for r in results)
    atomic_json(manifest_path, {'schema': 1, 'parent': parent, 'documents': docs, 'folders': folders, 'removed_remote': removed})
    return {'destination': str(destination), 'manifest': str(manifest_path), 'complete': not errors and not pending,
            'downloaded': downloaded, 'skipped': skipped, 'pending': pending, 'errors': errors,
            'removed_remote': removed, 'results': results, 'note': 'Remote removals are reported only; local archives are never deleted. Files are a best-effort snapshot, not an atomic whole-library snapshot.'}
