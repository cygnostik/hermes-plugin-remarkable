"""Product boundaries tested without touching the live Hermes home."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('product_tools', ROOT / 'tools.py')
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    monkeypatch.delenv('REMARKABLE_PLUGIN_DIR', raising=False)
    tools.set_config({})
    yield
    tools.set_config({})


def test_token_state_survives_install_relocation(tmp_path, monkeypatch):
    original = tools._resolve_token_file()
    monkeypatch.setattr(tools, '_PLUGIN_DIR', tmp_path / 'replacement-install')
    assert tools._resolve_token_file() == original
    assert Path(original).is_relative_to(tmp_path / 'hermes')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'other-profile'))
    assert tools._resolve_token_file() != original


@pytest.mark.parametrize('stdout,returncode', [('[]', 0), ('{"id": 2,"ok":true,"result":{}}', 0), ('{"id":1,"ok":true,"result":{}}', 9)])
def test_sidecar_protocol_rejects_invalid_success(monkeypatch, stdout, returncode):
    from types import SimpleNamespace
    monkeypatch.setattr(tools.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=stdout, stderr='', returncode=returncode))
    response = tools._call_sidecar('status', {})
    assert isinstance(response, dict) and response.get('ok') is False
    assert response['code'] in ('BAD_RESPONSE', 'SIDECAR_EXIT')


def test_enrollment_never_accepts_code_in_tool_args(monkeypatch):
    monkeypatch.setattr(tools, '_call_sidecar', lambda *a, **kw: pytest.fail('code must not be sent by a model tool'))
    assert json.loads(tools.remarkable_enroll({'code': 'abcd1234'}))['code'] == 'SECURE_INPUT_REQUIRED'
    instructions = json.loads(tools.remarkable_enroll({}))
    assert instructions['setup_command']
    assert 'chat' in instructions['instructions'].lower()


def test_cloud_transfer_preserves_folder_and_pagination(monkeypatch, tmp_path):
    calls = []
    def invoke(op, args, cfg=None, **kw):
        calls.append((op, args))
        return {'ok': True, 'result': {'entries': [], 'id': 'created'}}
    monkeypatch.setattr(tools, '_call_sidecar', invoke)
    tools.remarkable_list({'offset': 200, 'limit': 50, 'query': 'meeting', 'parent': None})
    assert calls[-1][1]['offset'] == 200
    assert calls[-1][1]['query'] == 'meeting'
    assert 'parent' not in calls[-1][1]
    document = tmp_path / 'test.pdf'
    document.write_bytes(b'%PDF-1.4 test')
    tools.remarkable_upload({'path': str(document), 'name': 'test', 'parent': 'scratch'})
    assert calls[-1][1]['parent'] == 'scratch'
    tools.remarkable_download({'id': 'doc', 'out_path': str(tmp_path / 'out.zip'), 'format': 'archive'})
    assert calls[-1][1]['format'] == 'archive'
    assert calls[-1][1]['overwrite'] is False


def test_management_requires_confirmation_and_dispatches_explicit_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(tools, '_call_sidecar', lambda op,args,cfg=None,**kw: calls.append((op,args)) or {'ok':True,'result':{'id':'folder'}})
    refused = json.loads(tools.remarkable_manage({'id':'personal','action':'trash'}))
    assert refused['code'] == 'CONFIRM_REQUIRED' and not calls
    made = json.loads(tools.remarkable_mkdir({'name':'scratch','parent':''}))
    assert made['id'] == 'folder' and calls[-1][0] == 'mkdir'
    moved = json.loads(tools.remarkable_manage({'id':'test','action':'move','parent':'scratch','confirm':True}))
    assert moved['id'] == 'folder' and calls[-1][1]['parent'] == 'scratch'
    bad = json.loads(tools.remarkable_status({'transport':'unknown'}))
    assert bad['code'] == 'BAD_TRANSPORT'


def test_backup_and_changes_are_integrated(monkeypatch, tmp_path):
    baselines = []
    def invoke(op,args,cfg=None,**kw):
        if op == 'list': return {'ok':True,'result':{'entries':[],'complete':True}}
        if op == 'changes':
            baselines.append(args.get('baseline'))
            return {'ok':True,'result':{'snapshot':{'doc':'version'},'added':['doc'],'modified':[],'removed':[], 'complete': True}}
        pytest.fail(op)
    monkeypatch.setattr(tools, '_call_sidecar', invoke)
    backup = json.loads(tools.remarkable_backup({'destination':str(tmp_path/'backup')}))
    assert backup['complete'] is True and Path(backup['manifest']).exists()
    first = json.loads(tools.remarkable_changes({}))
    second = json.loads(tools.remarkable_changes({}))
    assert baselines == [None, {'doc':'version'}]
    assert first['baseline_created'] is True and second['baseline_created'] is False


def test_export_downloads_archive_and_cleans_temporary_copy(monkeypatch, tmp_path):
    sources = []
    def invoke(op,args,cfg=None,**kw):
        assert op == 'download' and args['format'] == 'archive'
        Path(args['outPath']).write_bytes(b'archive')
        return {'ok':True,'result':{'written':args['outPath']}}
    def render(op, args, cfg):
        assert op == 'render' and Path(args['source']).is_file()
        sources.append(Path(args['source']))
        Path(args['out_path']).write_bytes(b'pdf')
        return {'written':args['out_path'],'pages':1}
    monkeypatch.setattr(tools, '_call_sidecar', invoke)
    monkeypatch.setattr(tools, '_renderer_call', render, raising=False)
    result = json.loads(tools.remarkable_export({'id':'document','out_path':str(tmp_path/'export.pdf')}))
    assert result['pages'] == 1
    assert all(not path.exists() for path in sources)


def test_registration_is_profile_isolated_and_matches_schemas(monkeypatch):
    import sys
    spec = importlib.util.spec_from_file_location('remarkable_integration_test', ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    class Context:
        def __init__(self, token): self.token=token; self.handlers={}; self.commands={}
        def get_config(self,key,default=None): return self.token if key=='token_file' else default
        def register_tool(self,**kwargs): self.handlers[kwargs['name']] = kwargs['handler']
        def register_cli_command(self,**kwargs): self.commands[kwargs['name']] = kwargs
        def register_skill(self,*args): pass
    a,b = Context('profile-A'), Context('profile-B')
    package.register(a); package.register(b)
    assert 'remarkable_export' in a.handlers and 'remarkable_backup' in a.handlers
    assert 'remarkable' in a.commands
    calls = []
    monkeypatch.setattr(package.tools, '_call_sidecar', lambda op,args,cfg=None,**kw: calls.append(cfg['token_file']) or {'ok':True,'result':{'cloud':'ok'}})
    a.handlers['remarkable_status']({}); b.handlers['remarkable_status']({}); a.handlers['remarkable_status']({})
    assert calls == ['profile-A','profile-B','profile-A']


def test_upload_requires_explicit_destination_before_network(monkeypatch,tmp_path):
    doc = tmp_path/'doc.pdf'; doc.write_bytes(b'%PDF-1.4 fixture')
    monkeypatch.setattr(tools, '_call_sidecar', lambda *a,**kw: pytest.fail('must not upload implicitly to root'))
    result = json.loads(tools.remarkable_upload({'path':str(doc),'name':'test'}))
    assert result['code'] == 'BAD_ARGS'


def test_typed_search_reports_unreadable_documents(monkeypatch):
    def extract(args, **kwargs):
        if args['id'] == 'bad': return json.dumps({'error':'unsupported page', 'code':'UNSUPPORTED'})
        return json.dumps({'text':'Planning Meeting notes'})
    monkeypatch.setattr(tools, 'remarkable_extract_text', extract)
    result = json.loads(tools.remarkable_search({'ids':['good','bad'], 'query':'meeting'}))
    assert result['complete'] is False
    assert result['matches'][0]['id'] == 'good'
    assert result['errors'][0]['id'] == 'bad'


def test_upload_timeout_is_unknown_outcome_not_retryable(monkeypatch,tmp_path):
    doc=tmp_path/'doc.pdf'; doc.write_bytes(b'%PDF-1.4 fixture')
    calls=[]
    def timeout(*a,**kw):
        calls.append(1)
        raise tools.subprocess.TimeoutExpired('node',1)
    monkeypatch.setattr(tools.subprocess,'run',timeout)
    result=json.loads(tools.remarkable_upload({'path':str(doc),'name':'test','parent':'scratch'}))
    assert result['code']=='WRITE_OUTCOME_UNKNOWN'
    assert len(calls)==1


def test_enrollment_status_uses_bound_profile_config(tmp_path):
    token=tmp_path/'fixture-token'; token.write_text('synthetic-fixture',encoding='utf-8')
    result=json.loads(tools.remarkable_enroll({},config={'token_file':str(token)}))
    assert result['enrolled'] is True


@pytest.mark.parametrize('details_field', ['result', 'details'])
def test_cloud_error_preserves_structured_ambiguity(monkeypatch, details_field):
    details = {'item_id': 'scratch-doc', 'outcome': 'unknown', 'retry_safe': False}
    monkeypatch.setattr(tools, '_call_sidecar', lambda *a, **kw: {
        'ok': False, 'code': 'WRITE_OUTCOME_UNKNOWN', 'error': 'Inspect before retrying', details_field: details})
    result = json.loads(tools.remarkable_manage({'id': 'scratch-doc', 'action': 'trash', 'confirm': True}))
    assert result['details'] == details
    assert result['code'] == 'WRITE_OUTCOME_UNKNOWN'


def test_changes_preserves_explicit_cloud_override(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tools, '_call_sidecar', lambda op, args, cfg, **kw: calls.append(op) or {
        'ok': True, 'result': {'snapshot': {'schema': 1, 'refs': []}, 'complete': True}})
    result = json.loads(tools.remarkable_changes(
        {'transport': 'cloud'}, config={'transport': 'ssh', 'state_dir': str(tmp_path / 'state')}))
    assert result.get('baseline_created') is True, result
    assert calls == ['changes']


@pytest.mark.parametrize('stdout', ['{}', '{"ok":false}', '{"ok":"true","text":"bad"}', '{"ok":true,"error":"failed"}'])
def test_renderer_invalid_or_failed_envelopes_are_not_success(monkeypatch, tmp_path, stdout):
    from types import SimpleNamespace
    runtime = tmp_path / 'python.exe'; runtime.touch()
    monkeypatch.setattr(tools.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=stdout))
    result = tools._renderer_call('extract', {'source': 'fixture.zip'}, {'renderer_python': str(runtime)})
    assert result.get('ok') is False, result
    assert result.get('complete') is False and result.get('error')


@pytest.mark.parametrize('mode,code', [('crash', 'RENDER_FAILED'), ('timeout', 'RENDER_TIMEOUT')])
def test_renderer_process_failure_is_explicit(monkeypatch, tmp_path, mode, code):
    from types import SimpleNamespace
    runtime = tmp_path / 'python.exe'; runtime.touch()
    def run(*args, **kwargs):
        if mode == 'timeout': raise tools.subprocess.TimeoutExpired('fixture', 1)
        return SimpleNamespace(returncode=9, stdout='{"ok":true}', stderr='native crash')
    monkeypatch.setattr(tools.subprocess, 'run', run)
    result = tools._renderer_call('render', {}, {'renderer_python': str(runtime)})
    assert result['code'] == code
    assert result.get('ok') is False and result.get('complete') is False


def test_missing_node_advice_matches_bundled_engine_requirement(monkeypatch):
    def missing(*args, **kwargs): raise FileNotFoundError('node')
    monkeypatch.setattr(tools.subprocess, 'run', missing)
    response = tools._call_sidecar('status', {})
    result = json.loads(tools.remarkable_status({}))
    assert 'Node 22+' in response['error']
    assert 'Node.js 22+' in result['error'] and '18+' not in result['error']










