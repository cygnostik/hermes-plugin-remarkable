"""Offline consumer release gates; no credentials or cloud calls.

These qualification tests cover existing behavior as well as CI admission.
The native worker gate is mandatory when REMARKABLE_TEST_RENDERER_PYTHON is set.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


class Context:
    def __init__(self, config=None):
        self.config = config or {}
        self.tools = {}
        self.commands = {}
        self.skills = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, **kw):
        assert kw['name'] not in self.tools
        self.tools[kw['name']] = kw

    def register_cli_command(self, **kw):
        self.commands[kw['name']] = kw

    def register_skill(self, name, path):
        self.skills[name] = path


def load_package(root, name):
    spec = importlib.util.spec_from_file_location(name, root / '__init__.py',
                                                submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return package


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profile'))
    monkeypatch.delenv('REMARKABLE_PLUGIN_DIR', raising=False)
    monkeypatch.delenv('RMAPI_JS_TOKEN_FILE', raising=False)
    monkeypatch.delenv('RMAPI_JS_CACHE_DIR', raising=False)


def test_registered_handlers_match_manifest_and_return_json_without_network(tmp_path, monkeypatch):
    package = load_package(ROOT, 'release_registration')
    ctx = Context({'transport': 'invalid-offline-transport'})
    package.register(ctx)
    manifest = (ROOT / 'plugin.yaml').read_text(encoding='utf-8')
    declared = re.findall(r'^  - (remarkable_\w+)$', manifest, re.MULTILINE)
    assert len(declared) == len(set(declared))
    assert set(ctx.tools) == set(declared)
    monkeypatch.setattr(package.tools, '_call_sidecar', lambda *a, **kw: pytest.fail('Unexpected cloud call'))
    for name, tool in ctx.tools.items():
        assert tool['schema']['name'] == name
        result = tool['handler']({}, session_id='offline-fixture')
        assert isinstance(result, str), name
        assert isinstance(json.loads(result), dict), name
    assert not (tmp_path / 'profile').exists(), 'Import/registration/invalid inputs must not create profile state'
    assert ctx.skills['tablet'].is_file()
    parser = argparse.ArgumentParser()
    ctx.commands['remarkable']['setup_fn'](parser)
    assert parser.parse_args(['setup-cloud']).remarkable_command == 'setup-cloud'


def copy_install(destination):
    destination.mkdir(parents=True)
    for source in ROOT.glob('*.py'):
        shutil.copy2(source, destination / source.name)
    shutil.copy2(ROOT / 'requirements-render.txt', destination / 'requirements-render.txt')
    return destination


def test_profile_state_survives_actual_install_replacement(tmp_path):
    install = copy_install(tmp_path / 'replaceable install')
    original = load_package(install, 'release_before_update')
    original.register(Context())
    token = Path(original.tools._resolve_token_file({}))
    state = original.tools._state_dir({})
    fixtures = {token: 'synthetic-device-token', Path(str(token) + '.session.json'): 'synthetic-session',
                state / 'changes.json': '{"fixture":"version"}', state / 'renderer-venv' / 'sentinel': 'runtime'}
    for path, content in fixtures.items():
        assert not path.is_relative_to(install)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    shutil.rmtree(install)
    copy_install(install)
    updated = load_package(install, 'release_after_update')
    updated.register(Context())
    assert Path(updated.tools._resolve_token_file({})) == token
    assert updated.tools._state_dir({}) == state
    assert {path: path.read_text(encoding='utf-8') for path in fixtures} == fixtures


def test_clean_import_and_help_need_no_renderer_dependencies(tmp_path):
    install = copy_install(tmp_path / 'clean install')
    result = subprocess.run([sys.executable, '-I', '-S', str(install / 'cli.py'), '--help'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'setup-renderer' in result.stdout


def test_external_native_worker_from_relocated_unicode_install(tmp_path):
    runtime = os.environ.get('REMARKABLE_TEST_RENDERER_PYTHON')
    if not runtime:
        pytest.skip('Set REMARKABLE_TEST_RENDERER_PYTHON for mandatory native qualification')
    assert Path(runtime).is_file(), 'Configured renderer must exist; never silently skip a broken runtime'
    install = copy_install(tmp_path / 'plugin with spaces ü')
    package = load_package(install, 'release_native_worker')
    ctx = Context({'renderer_python': runtime})
    package.register(ctx)
    source = tmp_path / 'notebook ü.rmdoc'
    shutil.copy2(ROOT / 'tests' / 'fixtures' / 'render-notebook.rmdoc', source)
    text = json.loads(ctx.tools['remarkable_extract_text']['handler']({'source': str(source)}))
    assert text['ok'] is True and text['complete'] is True, text
    assert 'Native text, no OCR.' in text['text']
    output = tmp_path / 'export ü.pdf'
    result = json.loads(ctx.tools['remarkable_export']['handler']({'source': str(source), 'out_path': str(output)}))
    assert result['ok'] is True and result['renderer'] == 'librm_lines', result
    assert output.read_bytes().startswith(b'%PDF-')
    assert not list((package.tools._state_dir({}) / 'work').iterdir())


def test_release_ci_covers_supported_native_runners_with_pinned_actions():
    # The real Hermes URL installer rejects version 2 even though validate/doctor accept it.
    assert 'manifest_version: 1' in (ROOT / 'plugin.yaml').read_text(encoding='utf-8')
    workflow = ROOT / '.github' / 'workflows' / 'ci.yml'
    assert workflow.is_file(), 'Release needs offline Python + Node + native CI'
    text = workflow.read_text(encoding='utf-8')
    for runner in ('windows-2022', 'ubuntu-24.04', 'macos-15', 'macos-15-intel'):
        assert runner in text
    actions = re.findall(r'uses:\s*([^\s]+)', text)
    assert actions and all(re.fullmatch(r'[^@]+@[0-9a-f]{40}', action) for action in actions)
    for required in ('setup-cloud', 'setup-renderer', 'REMARKABLE_TEST_RENDERER_PYTHON', 'npm test', '-m pytest'):
        assert required in text
    assert 'continue-on-error' not in text
    assert 'secrets.' not in text
