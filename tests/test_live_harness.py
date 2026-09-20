"""The live harness must be inert on import and refuse implicit writes."""
import importlib.util
from pathlib import Path
import subprocess
import pytest


def test_smoke_harness_is_inert_and_read_only_by_default(monkeypatch):
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('import must never access cloud'))
    path = Path(__file__).with_name('smoke_sidecar.py')
    spec = importlib.util.spec_from_file_location('rm_live_harness', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.make_parser().parse_args(['--token-file','fixture-token','--output-dir','fixture-output'])
    assert args.allow_writes is False
    assert not args.create_scratch and not args.scratch_folder


def test_failed_live_read_persists_actual_error(monkeypatch, tmp_path):
    import json
    import types
    spec = importlib.util.spec_from_file_location('rm_live_failure_test', Path(__file__).with_name('smoke_sidecar.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def atomic_json(path, data):
        path.write_text(json.dumps(data), encoding='utf-8')
    fake = types.SimpleNamespace(
        remarkable_status=lambda *a, **kw: json.dumps({'error':'rate limited','code':'HTTP_429'}),
        _module=lambda name: types.SimpleNamespace(atomic_json=atomic_json))
    monkeypatch.setattr(module.importlib.util, 'module_from_spec', lambda spec: fake)
    monkeypatch.setattr(module.importlib.util, 'spec_from_file_location', lambda *a: types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=lambda mod: None)))
    args = module.make_parser().parse_args(['--token-file','fixture-token','--output-dir',str(tmp_path)])
    with pytest.raises(RuntimeError):
        module.run(args)
    result = json.loads((tmp_path/'live-receipt.json').read_text())
    assert result['failed_operation']['result']['code'] == 'HTTP_429'
    assert result['failed_operation']['tool'] == 'status'

