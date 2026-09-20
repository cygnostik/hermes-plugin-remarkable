import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_cli():
    spec = importlib.util.spec_from_file_location('rm_cli_setup_test', ROOT / 'cli.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_setup_cloud_installs_locked_dependencies_without_scripts(monkeypatch):
    cli = load_cli()
    monkeypatch.setattr(cli.shutil, 'which', lambda name: name)
    calls = []
    monkeypatch.setattr(cli.subprocess, 'run', lambda cmd, **kw: calls.append((cmd, kw)) or subprocess.CompletedProcess(cmd, 0))
    assert cli.main(['setup-cloud']) == 0
    cmd, options = calls[0]
    assert cmd == ['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund']
    assert Path(options['cwd']) == ROOT / 'sidecar'
    assert not options.get('shell')


def test_renderer_setup_preserves_existing_environment(monkeypatch, tmp_path):
    cli = load_cli()
    env = tmp_path / 'renderer-venv'
    python = env / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    python.parent.mkdir(parents=True)
    python.write_bytes(b'fixture interpreter')
    monkeypatch.setattr(cli.shutil, 'which', lambda name: name)
    monkeypatch.setattr(cli.tools, '_state_dir', lambda cfg: tmp_path)
    calls = []
    monkeypatch.setattr(cli.subprocess, 'run', lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    assert cli.main(['setup-renderer']) == 0
    assert len(calls) == 1 and calls[0][1:3] == ['pip', 'install']


def test_renderer_setup_requires_supported_binary_wheels(monkeypatch, tmp_path):
    cli = load_cli()
    monkeypatch.setattr(cli.shutil, 'which', lambda name: name)
    monkeypatch.setattr(cli.tools, '_state_dir', lambda cfg: tmp_path)
    calls = []
    monkeypatch.setattr(cli.subprocess, 'run', lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    assert cli.main(['setup-renderer']) == 0
    install = calls[-1]
    assert '--only-binary' in install
    assert install[install.index('--only-binary') + 1] == ':all:'


def test_renderer_setup_launch_failure_is_actionable(monkeypatch, tmp_path, capsys):
    cli = load_cli()
    monkeypatch.setattr(cli.shutil, 'which', lambda name: name)
    monkeypatch.setattr(cli.tools, '_state_dir', lambda cfg: tmp_path)
    def unavailable(*args, **kwargs):
        raise OSError('synthetic launch failure')
    monkeypatch.setattr(cli.subprocess, 'run', unavailable)
    assert cli.main(['setup-renderer']) == 2
    assert 'cloud tools are unaffected' in capsys.readouterr().err.lower()


def test_clear_session_preserves_device_credential(monkeypatch, tmp_path):
    cli = load_cli()
    token = tmp_path / 'device-token'
    token.write_text('synthetic-device-credential')
    session = tmp_path / 'device-token.session.json'
    session.write_text('synthetic-session-credential')
    monkeypatch.setattr(cli.tools, '_resolve_token_file', lambda cfg=None: str(token))
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **kw: pytest.fail('clear-session must not access network'))
    assert cli.main(['clear-session']) == 0
    assert not session.exists() and token.read_text() == 'synthetic-device-credential'
    assert cli.main(['clear-session']) == 0


def test_cli_has_setup_and_no_plaintext_code_argument(tmp_path):
    proc = subprocess.run([sys.executable, str(ROOT / 'cli.py'), '--help'], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert 'enroll' in proc.stdout and 'setup-renderer' in proc.stdout
    proc = subprocess.run([sys.executable, str(ROOT / 'cli.py'), 'enroll', '--code', 'abcd1234'], capture_output=True, text=True)
    assert proc.returncode != 0


def test_enrollment_masked_and_token_not_printed(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('rm_cli_test', ROOT / 'cli.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    class TTY(io.StringIO):
        def isatty(self): return True
    monkeypatch.setattr(cli.sys, 'stdin', TTY())
    monkeypatch.setattr(cli.getpass, 'getpass', lambda *a: 'abcd1234')
    monkeypatch.setattr(cli.webbrowser, 'open', lambda *a: True)
    calls = []
    monkeypatch.setattr(cli.tools, '_call_sidecar', lambda op,args,cfg: calls.append((op,args)) or {'ok':True,'result':{'enrolled':True}})
    assert cli.main(['enroll']) == 0
    assert calls == [('enroll', {'code':'abcd1234'})]
    assert 'abcd1234' not in capsys.readouterr().out


def test_render_worker_rejects_unknown_operation_as_json():
    proc = subprocess.run([sys.executable, str(ROOT/'render_worker.py')], input=json.dumps({'op':'unknown','args':{}}), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)['code'] == 'BAD_OP'

