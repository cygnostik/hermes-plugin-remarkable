"""Backup exercises real filesystem state with an in-memory protocol boundary."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_incremental_backup_reuses_verified_file_and_reports_missing_items(tmp_path):
    spec = importlib.util.spec_from_file_location('rm_library_test', ROOT / 'library.py')
    library = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(library)
    state = {'entries': [
        {'id':'folder','type':'folder','name':'Folder','parent':''},
        {'id':'doc','type':'file:notebook','name':'Notes','parent':'folder','version':'version-one'}
    ]}
    downloads = []
    def call(op,args):
        if op == 'list': return {'ok':True,'result':dict(state, matched=len(state['entries']),truncated=False,complete=True)}
        if op == 'download':
            downloads.append(args['id'])
            Path(args['outPath']).write_bytes(b'archive-test-content')
            return {'ok':True,'result':{'written':args['outPath'],'bytes':20,'kind':'archive'}}
        raise AssertionError(op)
    dest = tmp_path / 'backup'
    first = library.backup(call, dest, parent='folder')
    assert first['complete'] is True and first['downloaded'] == 1
    second = library.backup(call, dest, parent='folder')
    assert second['skipped'] == 1 and downloads == ['doc']
    state['entries'] = []
    third = library.backup(call, dest, parent='folder')
    assert third['removed_remote'] == ['doc']
    assert (dest / 'doc.zip').exists(), 'backups must never delete local copies'


def test_backup_refuses_concurrent_writer(tmp_path):
    import pytest
    spec = importlib.util.spec_from_file_location('rm_library_lock_test', ROOT/'library.py')
    library=importlib.util.module_from_spec(spec); spec.loader.exec_module(library)
    (tmp_path/'.backup.lock').write_text('active operation',encoding='utf-8')
    with pytest.raises(RuntimeError, match='lock'):
        library.backup(lambda *a: pytest.fail('must not call cloud while backup is locked'), tmp_path)


def test_scoped_backup_manifest_omits_unrelated_folder_metadata(tmp_path):
    import json
    spec = importlib.util.spec_from_file_location('rm_library_scope_test', ROOT / 'library.py')
    library = importlib.util.module_from_spec(spec); spec.loader.exec_module(library)
    entries = [
        {'id': 'scope', 'type': 'folder', 'name': 'Scratch', 'parent': ''},
        {'id': 'child', 'type': 'folder', 'name': 'Child', 'parent': 'scope'},
        {'id': 'unrelated', 'type': 'folder', 'name': 'Private unrelated folder', 'parent': ''},
    ]
    result = library.backup(lambda *a: {'ok': True, 'result': {'entries': entries, 'complete': True}}, tmp_path, parent='scope')
    assert result['complete'] is True
    manifest = json.loads((tmp_path / 'manifest.json').read_text())
    assert {e['id'] for e in manifest['folders']} == {'scope', 'child'}


def test_backup_preserves_immutable_cache_and_follows_cloud_cursor(tmp_path):
    spec = importlib.util.spec_from_file_location('rm_library_cursor_test', ROOT / 'library.py')
    library = importlib.util.module_from_spec(spec); spec.loader.exec_module(library)
    calls = []
    def call(op, args):
        calls.append(dict(args))
        entry = {'id': 'folder-' + str(len(calls)), 'type': 'folder', 'parent': ''}
        return {'ok': True, 'result': {'entries': [entry], 'matched': 2, 'complete': True,
            'has_more': len(calls) == 1, 'next_offset': 1 if len(calls) == 1 else None,
            'next_cursor': 'opaque-cloud-cursor' if len(calls) == 1 else None}}
    result = library.backup(call, tmp_path)
    assert result['complete'] is True and len(calls) == 2
    assert not any(args.get('refresh') for args in calls), 'Cloud root is always fresh; immutable cache need not be cleared'
    assert calls[1]['cursor'] == 'opaque-cloud-cursor'


def test_backup_stops_on_repeated_pages_without_claiming_remote_removals(tmp_path):
    import json
    spec = importlib.util.spec_from_file_location('rm_library_repeated_test', ROOT / 'library.py')
    library = importlib.util.module_from_spec(spec); spec.loader.exec_module(library)
    (tmp_path / 'manifest.json').write_text(json.dumps({'schema': 1, 'parent': None, 'documents': {'old-doc': {}}}))
    calls = []
    def call(op, args):
        calls.append(args)
        return {'ok': True, 'result': {'entries': [{'id': 'folder', 'type': 'folder', 'parent': ''}],
                                     'matched': 2, 'truncated': True}}
    result = library.backup(call, tmp_path)
    assert len(calls) == 2, 'Repeated pages must not cause 1000 duplicate network walks'
    assert result['complete'] is False and result['removed_remote'] == []
    assert any(error['code'] == 'LIBRARY_CHANGED' for error in result['errors'])


def test_backup_rejects_unfulfilled_listing_total(tmp_path):
    import json
    spec = importlib.util.spec_from_file_location('rm_library_total_test', ROOT / 'library.py')
    library = importlib.util.module_from_spec(spec); spec.loader.exec_module(library)
    (tmp_path / 'manifest.json').write_text(json.dumps({'schema': 1, 'parent': None, 'documents': {'old-doc': {}}}))
    result = library.backup(lambda *a: {'ok': True, 'result': {'entries': [], 'matched': 1, 'complete': True}}, tmp_path)
    assert result['complete'] is False and result['removed_remote'] == []
    assert result['errors'][0]['code'] == 'PARTIAL_LIBRARY'

