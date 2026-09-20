"""Consumer recovery when an update replaces locally installed dependencies."""
import importlib.util
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_updated_source_without_dependencies_gives_repair_command(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('update_tools', ROOT / 'tools.py')
    tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tools)
    sidecar = tmp_path / 'updated-plugin' / 'sidecar'
    sidecar.mkdir(parents=True)
    for path in (ROOT / 'sidecar').glob('*.mjs'):
        shutil.copyfile(path, sidecar / path.name)
    monkeypatch.setattr(tools, '_SIDECAR', sidecar / 'sidecar.mjs')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'isolated-profile'))
    response = tools._call_sidecar('status', {})
    assert response['ok'] is False
    assert response['code'] == 'CLOUD_SETUP_REQUIRED', response
    assert 'hermes remarkable setup-cloud' in response['error']
