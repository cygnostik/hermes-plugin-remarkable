"""Offline tests for the remarkable plugin's Python layer (no network, stub sidecar)."""

import json
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rm_tools", _ROOT / "tools.py")
tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tools)


class FakeProc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def run_ok(monkeypatch, result, cfg=None):
    calls = {}

    def fake_run(cmd, input=None, **kw):
        calls["cmd"] = cmd
        calls["input"] = input
        calls["env"] = kw.get("env")
        return FakeProc(json.dumps({"id": 1, "ok": True, "result": result}) + "\n")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    return calls


def test_token_file_resolution(monkeypatch):
    tools.set_config({})
    monkeypatch.delenv("REMARKABLE_PLUGIN_DIR", raising=False)
    default = tools._resolve_token_file()
    assert Path(default).parts[-3:] == ("credentials", "remarkable", "device-token")

    monkeypatch.setenv("REMARKABLE_PLUGIN_DIR", "X:/tmp/rmstate")
    assert tools._resolve_token_file().replace("\\", "/") == "X:/tmp/rmstate/device-token"

    tools.set_config({"token_file": "X:/cfg/token"})
    assert tools._resolve_token_file().replace("\\", "/") == "X:/cfg/token"
    tools.set_config({})


def test_status_passes_through(monkeypatch):
    calls = run_ok(monkeypatch, {"enrolled": True, "library": 12, "cloud": "ok"})
    out = json.loads(tools.remarkable_status({}))
    assert out == {"enrolled": True, "library": 12, "cloud": "ok"}
    req = json.loads(calls["input"].strip())
    assert req["op"] == "status"


@pytest.mark.parametrize('code', ['short', 'abcd1234'])
def test_enroll_redirects_all_codes_to_secure_local_prompt(monkeypatch, code):
    out = json.loads(tools.remarkable_enroll({'code': code}))
    assert out['code'] == 'SECURE_INPUT_REQUIRED'
    assert code not in out['error']


def test_list_maps_args(monkeypatch):
    calls = run_ok(monkeypatch, {"entries": [{"id": "x", "name": "n", "type": "folder"}], "totalRefs": 1})
    out = json.loads(tools.remarkable_list({"parent": "", "limit": 500, "refresh": True}))
    req = json.loads(calls["input"].strip())
    assert req["args"]["parent"] == ""
    assert req["args"]["limit"] == 200  # clamped
    assert req["args"]["refresh"] is True
    assert out["entries"][0]["name"] == "n"


def test_download_requires_both_args(monkeypatch):
    out = json.loads(tools.remarkable_download({"id": "abc"}))
    assert out["code"] == "BAD_ARGS"


def test_upload_rejects_missing_file(monkeypatch):
    out = json.loads(tools.remarkable_upload({"path": "Z:/nope/nothing.pdf", "name": "x"}))
    assert out["code"] == "NO_FILE"


def test_upload_rejects_unknown_kind(monkeypatch, tmp_path):
    f = tmp_path / "thing.xyz"
    f.write_text("data")
    out = json.loads(tools.remarkable_upload({"path": str(f), "name": "x"}))
    assert out["code"] == "BAD_KIND"


def test_upload_infers_kind_and_calls_sidecar(monkeypatch, tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    calls = run_ok(monkeypatch, {"id": "newid", "name": "doc", "bytes": 13})
    out = json.loads(tools.remarkable_upload({"path": str(f), "name": "doc", "parent": ""}))
    req = json.loads(calls["input"].strip())
    assert req["op"] == "upload_pdf"
    assert out["kind"] == "pdf"
    assert out["id"] == "newid"


def test_error_response_gets_hint(monkeypatch):
    def fake_run(cmd, input=None, **kw):
        return FakeProc(json.dumps({"id": 1, "ok": False, "error": "not enrolled", "code": "NO_TOKEN"}) + "\n")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    out = json.loads(tools.remarkable_status({}))
    assert out["code"] == "NO_TOKEN"
    assert "remarkable enroll" in out["error"]


def test_no_response_from_sidecar(monkeypatch):
    def fake_run(cmd, input=None, **kw):
        return FakeProc("", returncode=1, stderr="boom")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    out = json.loads(tools.remarkable_status({}))
    assert out["code"] == "NO_RESPONSE"
    assert "boom" in out["error"]


def test_missing_node(monkeypatch):
    def fake_run(cmd, input=None, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    out = json.loads(tools.remarkable_status({}))
    assert out["code"] == "NO_NODE"
