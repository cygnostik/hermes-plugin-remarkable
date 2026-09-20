"""Offline cross-component contracts; never use an account or real tablet."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename, package=False):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / filename, submodule_search_locations=[str(ROOT)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tools = load('integration_contract_tools', 'tools.py')


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    monkeypatch.delenv('REMARKABLE_PLUGIN_DIR', raising=False)
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    tools.set_config({})
    monkeypatch.setattr(tools, '_call_sidecar', lambda *a, **kw: pytest.fail('Unexpected cloud request'))
    yield
    tools.set_config({})


def test_registration_exposes_explicit_local_opt_ins(monkeypatch):
    package = load('integration_contract_package', '__init__.py', package=True)

    class Context:
        handlers = {}
        def get_config(self, key, default=None):
            return True if key in ('usb_enabled', 'usb_allow_upload', 'ssh_enabled') else default
        def register_tool(self, **kw): self.handlers[kw['name']] = kw['handler']
        def register_cli_command(self, **kw): pass
        def register_skill(self, *args): pass

    ctx = Context()
    package.register(ctx)
    config = ctx.handlers['remarkable_status'].keywords['config']
    for key in ('usb_enabled', 'usb_allow_upload', 'ssh_enabled'):
        assert config.get(key) is True, key
        assert package.CONFIG_DEFAULTS[key] is False
        assert f'  {key}:\n    type: bool\n    default: false' in (ROOT / 'plugin.yaml').read_text()
    assert config['transport'] == 'cloud'
    assert not config.get('ssh_identity'), 'An absent optional SSH identity must not become an empty path'


@pytest.fixture
def usb():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    state = {'entries': [], 'requests': [], 'archive': b''}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            state['requests'].append(('GET', self.path))
            data = json.dumps(state['entries']).encode() if self.path == '/documents/' else state['archive']
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            state['requests'].append(('POST', self.path))
            state['upload'] = self.rfile.read(int(self.headers['Content-Length']))
            state['entries'].append({'ID': '44444444-4444-4444-8444-444444444444',
                                     'VissibleName': 'Fixture', 'Type': 'DocumentType', 'Parent': ''})
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {'usb_enabled': True, 'usb_allow_upload': True,
              'usb_url': f'http://127.0.0.1:{server.server_port}'}
    try:
        yield state, config
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_upload_tool_reaches_real_usb_upload_contract(usb, tmp_path):
    state, config = usb
    source = tmp_path / 'fixture.pdf'
    source.write_bytes(b'%PDF-1.4 synthetic integration fixture')
    result = json.loads(tools.remarkable_upload(
        {'path': str(source), 'name': 'Fixture', 'parent': '', 'transport': 'usb'}, config=config))
    assert result.get('verified') is True, result
    assert state['requests'] == [('GET', '/documents/'), ('POST', '/upload'), ('GET', '/documents/')]
    assert source.read_bytes() in state['upload']
    epub = tmp_path / 'fixture.epub'
    epub.write_bytes(b'not sent')
    result = json.loads(tools.remarkable_upload(
        {'path': str(epub), 'name': 'Fixture', 'parent': '', 'transport': 'usb'}, config=config))
    assert result['code'] == 'UNSUPPORTED'
    assert len(state['requests']) == 3


def test_local_listing_filters_before_paginating_and_reports_coverage(usb):
    state, config = usb
    state['entries'] = [
        {'ID': f'{n:08x}-1111-4111-8111-111111111111',
         'VissibleName': 'Meeting' if n % 2 else 'Other', 'Type': 'DocumentType'}
        for n in range(405, 0, -1)]
    first = json.loads(tools.remarkable_list({'transport': 'usb', 'query': 'meeting', 'limit': 200}, config=config))
    assert first['matched'] == 203
    assert first['complete'] is True and first['has_more'] is True
    assert first['next_offset'] == 200
    second = json.loads(tools.remarkable_list(
        {'transport': 'usb', 'query': 'meeting', 'limit': 200, 'offset': first['next_offset']}, config=config))
    assert len(second['entries']) == 3 and second['has_more'] is False
    ids = [entry['id'] for entry in first['entries'] + second['entries']]
    assert ids == sorted(set(ids)) and len(ids) == 203
    assert second['next_offset'] is None
    assert second['search_scope'] == 'names_only'


@pytest.mark.parametrize('option', ['include_tags', 'include_trash'])
def test_local_listing_rejects_unsupported_coverage_options(usb, option):
    state, config = usb
    result = json.loads(tools.remarkable_list({'transport': 'usb', option: True}, config=config))
    assert result['code'] == 'UNSUPPORTED'
    assert not state['requests']


def test_download_defaults_follow_transport_without_relabeling_usb_pdf(usb, tmp_path):
    import io
    import zipfile
    state, config = usb
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('fixture.content', '{}')
    state['archive'] = data.getvalue()
    doc_id = '11111111-1111-4111-8111-111111111111'
    result = json.loads(tools.remarkable_download(
        {'id': doc_id, 'out_path': str(tmp_path / 'archive.zip'), 'transport': 'usb'}, config=config))
    assert result.get('kind') == 'rmdoc', result
    assert (tmp_path / 'archive.zip').read_bytes() == state['archive']
    assert state['requests'][-1][1] == f'/download/{doc_id}/rmdoc'
    state['archive'] = b'%PDF-1.4 synthetic device export'
    result = json.loads(tools.remarkable_download(
        {'id': doc_id, 'out_path': str(tmp_path / 'device.pdf'), 'format': 'pdf', 'transport': 'usb'}, config=config))
    assert result.get('kind') == 'pdf', result
    schemas = load('integration_contract_schemas', 'schemas.py')
    assert 'pdf' in schemas.DOWNLOAD['parameters']['properties']['format']['enum']
    result = json.loads(tools.remarkable_download(
        {'id': doc_id, 'out_path': str(tmp_path / 'original.pdf'), 'format': 'original', 'transport': 'usb'}, config=config))
    assert result['code'] == 'UNSUPPORTED'
    assert len(state['requests']) == 2


def test_real_worker_download_render_extract_and_search(usb, tmp_path):
    import os
    runtime = Path(os.environ.get('REMARKABLE_TEST_RENDERER_PYTHON',
        str(ROOT.parent / 'runtime' / 'remarkable-render' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python'))))
    if not runtime.is_file():
        pytest.skip('Set REMARKABLE_TEST_RENDERER_PYTHON to the external renderer interpreter')
    pymupdf = pytest.importorskip('pymupdf')
    state, local_config = usb
    source = ROOT / 'tests' / 'fixtures' / 'render-notebook.rmdoc'
    state['archive'] = source.read_bytes()
    cfg = dict(local_config, renderer_python=str(runtime), state_dir=str(tmp_path / 'state'))
    doc_id = '11111111-1111-4111-8111-111111111111'
    target = tmp_path / 'worker.pdf'
    result = json.loads(tools.remarkable_export(
        {'id': doc_id, 'transport': 'usb', 'out_path': str(target)}, config=cfg))
    assert result.get('ok') is True and result['renderer'] == 'librm_lines', result
    with pymupdf.open(target) as doc:
        assert len(doc) == 2
        assert min(doc[0].get_pixmap().samples) < 100
    text = json.loads(tools.remarkable_extract_text({'source': str(source)}, config=cfg))
    assert text['ok'] is True and text['complete'] is True and text['ocr'] is False
    assert [page['id'] for page in text['pages']] == ['first', 'second']
    assert text['text'].startswith('First typed page\nNative text, no OCR.')
    found = json.loads(tools.remarkable_search({'ids': [doc_id], 'query': 'Native text', 'transport': 'usb'}, config=cfg))
    assert found['complete'] is True and found['matches'][0]['id'] == doc_id
    assert not list((tmp_path / 'state' / 'work').iterdir()), 'Downloaded temporary archives must be cleaned'
    malformed = tmp_path / 'malformed.zip'; malformed.write_bytes(b'not a zip')
    failed = json.loads(tools.remarkable_extract_text({'source': str(malformed)}, config=cfg))
    assert failed['ok'] is False and failed['complete'] is False and failed['code'] == 'RENDER_ERROR'


def test_blank_optional_ssh_identity_uses_existing_keys(monkeypatch):
    local = tools._module('local_transport')
    calls = []
    monkeypatch.setattr(local._SSH, 'run', lambda self, commands, cwd: calls.append(self.command) or '')
    result = json.loads(tools.remarkable_status({'transport': 'ssh'}, config={
        'ssh_enabled': True, 'ssh_host': 'fixture.invalid', 'ssh_identity': ''}))
    assert result.get('reachable') is True, result
    assert len(calls) == 1 and '-i' not in calls[0]
    assert 'StrictHostKeyChecking=yes' in calls[0]


def test_local_backup_can_resume_without_overwriting_transport_outputs(usb, tmp_path):
    state, config = usb
    doc_id = '11111111-1111-4111-8111-111111111111'
    state['entries'] = [{'ID': doc_id, 'VissibleName': 'Fixture', 'Type': 'DocumentType'}]
    state['archive'] = (ROOT / 'tests' / 'fixtures' / 'render-notebook.rmdoc').read_bytes()
    args = {'transport': 'usb', 'destination': str(tmp_path / 'backup')}
    first = json.loads(tools.remarkable_backup(args, config=config))
    assert first['complete'] is True, first
    second = json.loads(tools.remarkable_backup(args, config=config))
    assert second['complete'] is True, second
    assert second['downloaded'] == 1, 'Local entries do not supply immutable versions'
    assert (tmp_path / 'backup' / (doc_id + '.zip')).read_bytes() == state['archive']
    state['archive'] = b'%PDF-1.4 wrong archive from old USB firmware'
    failed = json.loads(tools.remarkable_backup(args, config=config))
    assert failed['complete'] is False
    assert (tmp_path / 'backup' / (doc_id + '.zip')).read_bytes() != state['archive']
    assert sorted(path.name for path in (tmp_path / 'backup').iterdir()) == [doc_id + '.zip', 'manifest.json']
