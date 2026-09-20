"""Tool handlers — drive the Node sidecar over stdio.

All handlers follow the plugin contract: accept (args, **kwargs), never raise,
always return a JSON string. The sidecar (rmapi-js v14, MIT) holds the cloud
protocol knowledge; this layer owns lifecycle, timeouts and argument mapping.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
_SIDECAR = _PLUGIN_DIR / "sidecar" / "sidecar.mjs"
_DEFAULT_TIMEOUT_MS = 180_000

# Populated by register(ctx) from ctx.get_config(); handlers read it.
_CONFIG: dict = {}


def set_config(cfg: dict) -> None:
    """Called from register() with the plugin's resolved config values."""
    _CONFIG.clear()
    _CONFIG.update({k: v for k, v in (cfg or {}).items() if v is not None})


def _resolve_token_file(cfg: dict | None = None) -> str:
    """Explicit token path, legacy state override, then durable profile credentials."""
    cfg = cfg if cfg is not None else _CONFIG
    configured = (cfg or {}).get("token_file")
    if configured:
        return str(configured)
    env_dir = os.environ.get("REMARKABLE_PLUGIN_DIR")
    if env_dir:
        return str(Path(env_dir) / "device-token")
    return str(_hermes_home() / "credentials" / "remarkable" / "device-token")


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except ImportError:
        configured = os.environ.get("HERMES_HOME")
        if configured:
            return Path(configured).expanduser()
        if sys.platform == "win32":
            return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "hermes"
        return Path.home() / ".hermes"


def _state_dir(cfg=None) -> Path:
    cfg = _CONFIG if cfg is None else cfg
    return Path(cfg["state_dir"]).expanduser() if cfg.get("state_dir") else _hermes_home() / "plugin-data" / "remarkable"


def _node_bin(cfg: dict | None = None) -> str:
    return (cfg or {}).get("node_path") or os.environ.get("REMARKABLE_NODE_PATH") or "node"


def _call_sidecar(op: str, args: dict, cfg: dict | None = None, timeout_ms: int | None = None) -> dict:
    """Run one op against a fresh sidecar process. Returns the parsed response dict."""
    cfg = cfg or {}
    timeout_ms = timeout_ms or int(cfg.get("timeout_ms") or _DEFAULT_TIMEOUT_MS)
    if not _SIDECAR.exists():
        return {"ok": False, "error": f"sidecar missing: {_SIDECAR}", "code": "SIDECAR_MISSING"}

    env = dict(os.environ)
    env["RMAPI_JS_TOKEN_FILE"] = _resolve_token_file(cfg)
    env["RMAPI_JS_CACHE_DIR"] = str(_state_dir(cfg))

    request = json.dumps({"id": 1, "op": op, "args": args, "timeoutMs": timeout_ms})
    try:
        proc = subprocess.run(
            [_node_bin(cfg), str(_SIDECAR)],
            input=request + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=(timeout_ms / 1000.0) + 30,
            cwd=str(_SIDECAR.parent),
            env=env,
        )
    except subprocess.TimeoutExpired:
        if op in ('upload_pdf', 'upload_epub', 'mkdir', 'manage', 'enroll'):
            return {"ok": False, "error": "The write may have completed remotely. Inspect the destination before retrying; do not automatically repeat this operation.", "code": "WRITE_OUTCOME_UNKNOWN"}
        return {"ok": False, "error": f"sidecar timed out after {timeout_ms}ms", "code": "TIMEOUT"}
    except FileNotFoundError:
        return {"ok": False, "error": f"node executable not found: {_node_bin(cfg)} (Node 22+ required; set node_path in plugin config)", "code": "NO_NODE"}

    stdout = (proc.stdout or "").strip()
    if not stdout:
        if proc.returncode and 'ERR_MODULE_NOT_FOUND' in (proc.stderr or ''):
            return {"ok": False, "code": "CLOUD_SETUP_REQUIRED", "error": "Cloud dependencies are missing or incomplete. Run hermes remarkable setup-cloud after installing or updating this plugin, then retry a read. No cloud operation was dispatched by this failed module load."}
        stderr_tail = " ".join((proc.stderr or "").split())[-400:]
        return {"ok": False, "error": f"sidecar produced no response (exit {proc.returncode})" + (f": {stderr_tail}" if stderr_tail else ""), "code": "NO_RESPONSE"}

    # last JSON line is our response (sidecar may print debug lines earlier)
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict) or payload.get("id") != 1 or type(payload.get("ok")) is not bool:
                return {"ok": False, "error": "Invalid sidecar response envelope", "code": "BAD_RESPONSE"}
            if payload["ok"] and not isinstance(payload.get("result"), dict):
                return {"ok": False, "error": "Invalid sidecar result", "code": "BAD_RESPONSE"}
            if proc.returncode and payload["ok"]:
                return {"ok": False, "error": "Sidecar exited unsuccessfully; verify remote state before retrying writes", "code": "SIDECAR_EXIT"}
            return payload
        except json.JSONDecodeError:
            continue
    return {"ok": False, "error": "sidecar response was not JSON", "code": "BAD_RESPONSE"}


def _err(payload: dict) -> dict:
    code = payload.get("code", "ERROR")
    hint = ""
    if code == "NO_TOKEN":
        hint = " Run hermes remarkable enroll in a local terminal; enter the pairing code in its masked prompt, never in chat."
    elif code == "NO_NODE":
        hint = " Install Node.js 22+ or set node_path in the plugin config."
    result = {"error": str(payload.get("error", "unknown")) + hint, "code": code}
    # The cloud protocol carries structured failure details in result, whereas
    # some adapters expose details directly. Do not drop retry-safety evidence.
    details = payload.get('details', payload.get('result'))
    if isinstance(details, dict) and details:
        result['details'] = dict(details)
    return result


def _module(name):
    import importlib
    import importlib.util
    if __package__:
        return importlib.import_module('.' + name, __package__)
    key = '_remarkable_plugin_' + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, _PLUGIN_DIR / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def _local_list(transport, args, cfg):
    # Local transports walk the bounded library but do not filter or paginate.
    # Fetch it once before applying the public tool's name query and offset.
    if args.get('include_tags') or args.get('include_trash') or args.get('cursor'):
        return {'ok': False, 'code': 'UNSUPPORTED', 'error': 'Local listing supports name search and offsets only, not tags, trash, or cloud cursors.'}
    limit = max(1, min(int(args.get('limit', 60)), 200))
    offset = max(0, int(args.get('offset', 0)))
    request = {'limit': 10000}
    if 'parent' in args:
        request['parent'] = args['parent']
    response = _module('local_transport').call_local(transport, 'list', request, cfg)
    if not response.get('ok'):
        return response
    result = response['result']
    entries = result['entries']
    if result.get('truncated') or result.get('matched') != len(entries):
        return {'ok': False, 'code': 'PARTIAL_LIBRARY', 'error': 'Local library walk was incomplete; refusing to paginate incomplete data.'}
    query = str(args.get('query', '')).strip().casefold()
    selected = sorted((entry for entry in entries if query in str(entry.get('name', '')).casefold()), key=lambda entry: entry['id'])
    page = selected[offset:offset + limit]
    more = offset + len(page) < len(selected)
    return {'ok': True, 'result': dict(result, entries=page, matched=len(selected),
            offset=offset, limit=limit, has_more=more, truncated=more,
            next_offset=offset + len(page) if more else None, complete=True,
            search_scope='names_only')}


def _dispatch(op, args, cfg=None, **kwargs):
    cfg = dict(_CONFIG if cfg is None else cfg)
    args = dict(args)
    transport = args.pop('transport', None) or cfg.get('transport', 'cloud')
    if transport == 'cloud':
        return _call_sidecar(op, args, cfg, **kwargs)
    if transport in ('usb', 'ssh'):
        if cfg.get('ssh_identity') == '':
            cfg.pop('ssh_identity')
        if op == 'list':
            return _local_list(transport, args, cfg)
        if op in ('upload_pdf', 'upload_epub'):
            args['kind'] = op.removeprefix('upload_')
            op = 'upload'
        return _module('local_transport').call_local(transport, op, args, cfg)
    return {'ok': False, 'error': 'Choose cloud, usb, or ssh explicitly; writes never fall back automatically.', 'code': 'BAD_TRANSPORT'}


def _simple(op, args, kwargs):
    try:
        result = _dispatch(op, args, kwargs.get('config', _CONFIG))
        return json.dumps(result.get('result', {}) if result.get('ok') else _err(result))
    except Exception as exc:
        return json.dumps({'error': str(exc), 'code': 'INTERNAL'})


def remarkable_mkdir(args, **kwargs):
    if not str(args.get('name', '')).strip():
        return json.dumps({'error': 'name required', 'code': 'BAD_ARGS'})
    return _simple('mkdir', args, kwargs)


def remarkable_manage(args, **kwargs):
    if args.get('confirm') is not True:
        return json.dumps({'error': 'Confirm the specific document change with the user first.', 'code': 'CONFIRM_REQUIRED'})
    if args.get('action') not in ('rename', 'move', 'trash', 'restore') or not args.get('id'):
        return json.dumps({'error': 'id and action rename/move/trash/restore required; permanent deletion is not supported.', 'code': 'BAD_ARGS'})
    return _simple('manage', args, kwargs)


def remarkable_backup(args, **kwargs):
    try:
        if not args.get('destination'):
            return json.dumps({'error': 'destination required', 'code': 'BAD_ARGS'})
        cfg = kwargs.get('config', _CONFIG)
        def invoke(op, payload):
            return _dispatch(op, dict(payload, transport=args.get('transport')), cfg)
        return json.dumps(_module('library').backup(invoke, args['destination'], args.get('parent'), args.get('max_items', 200)))
    except Exception as exc:
        return json.dumps({'error': str(exc), 'code': 'BACKUP_FAILED'})


def remarkable_changes(args, **kwargs):
    try:
        cfg = kwargs.get('config', _CONFIG)
        if (args.get('transport') or cfg.get('transport', 'cloud')) != 'cloud':
            return json.dumps({'error': 'Change snapshots are supported by cloud transport only.', 'code': 'UNSUPPORTED'})
        state = _state_dir(cfg) / 'changes.json'
        old = json.loads(state.read_text(encoding='utf-8')) if state.is_file() and not args.get('reset') else None
        response = _dispatch('changes', {'baseline': old, 'transport': 'cloud'}, cfg)
        if not response.get('ok'):
            return json.dumps(_err(response))
        result = response['result']
        if result.get('complete', True) and 'snapshot' in result:
            _module('library').atomic_json(state, result['snapshot'])
        result['baseline_created'] = old is None
        # Protocol version IDs stay in durable state, not the conversational payload.
        result.pop('snapshot', None)
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({'error': str(exc), 'code': 'CHANGES_FAILED'})


def _renderer_call(op, args, cfg):
    runtime = cfg.get('renderer_python') or str(_state_dir(cfg) / 'renderer-venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python'))
    if not Path(runtime).is_file():
        return {'ok': False, 'complete': False, 'error': 'Run hermes remarkable setup-renderer, or configure renderer_python to a Python with requirements-render.txt installed.', 'code': 'RENDERER_MISSING'}
    try:
        result = subprocess.run([runtime, str(_PLUGIN_DIR / 'render_worker.py')],
                                input=json.dumps({'op': op, 'args': args}), capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=int(cfg.get('render_timeout', 180)))
        if result.returncode:
            return {'ok': False, 'complete': False, 'error': 'Rendering worker failed; inspect any output before retrying. Original document was not changed.', 'code': 'RENDER_FAILED'}
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError('Invalid renderer response')
        if payload.get('error'):
            return dict(payload, ok=False, complete=False)
        if type(payload.get('ok')) is not bool or payload['ok'] is False:
            raise ValueError('Invalid renderer response')
        return payload
    except subprocess.TimeoutExpired:
        return {'ok': False, 'complete': False, 'error': 'Rendering timed out; inspect any output before retrying. Original document was not changed.', 'code': 'RENDER_TIMEOUT'}
    except (OSError, ValueError):
        return {'ok': False, 'complete': False, 'error': 'Renderer could not start or returned an invalid response. Re-run setup-renderer.', 'code': 'RENDER_FAILED'}


def _document_operation(op, args, kwargs):
    import tempfile
    try:
        cfg = kwargs.get('config', _CONFIG)
        if not args.get('source') and not args.get('id'):
            return json.dumps({'error': 'Provide source archive path or document id', 'code': 'BAD_ARGS'})
        if op == 'render' and not args.get('out_path'):
            return json.dumps({'error': 'out_path required', 'code': 'BAD_ARGS'})
        work = _state_dir(cfg) / 'work'
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='export-', dir=work) as temp:
            source = args.get('source')
            if not source:
                source = str(Path(temp) / 'document.zip')
                response = _dispatch('download', {'id': args['id'], 'outPath': source, 'format': 'archive', 'overwrite': False, 'transport': args.get('transport')}, cfg)
                if not response.get('ok'):
                    return json.dumps(_err(response))
            payload = {'source': str(source)}
            if op == 'render':
                payload.update(out_path=args['out_path'], format=args.get('format', 'pdf'), overwrite=args.get('overwrite') is True, pages=args.get('pages'))
            return json.dumps(_renderer_call(op, payload, cfg))
    except Exception as exc:
        return json.dumps({'error': str(exc), 'code': 'EXPORT_FAILED'})


def remarkable_export(args, **kwargs):
    return _document_operation('render', args, kwargs)


def remarkable_extract_text(args, **kwargs):
    return _document_operation('extract', args, kwargs)


def remarkable_search(args, **kwargs):
    try:
        ids = args.get('ids')
        query = str(args.get('query', '')).strip()
        if not query or not isinstance(ids, list) or not 1 <= len(ids) <= 50 or not all(isinstance(x, str) and x for x in ids):
            return json.dumps({'error': 'query and 1-50 document ids required. Use remarkable_list first to select candidates.', 'code': 'BAD_ARGS'})
        matches, errors, searched = [], [], []
        for doc_id in dict.fromkeys(ids):
            result = json.loads(remarkable_extract_text({'id': doc_id, 'transport': args.get('transport')}, **kwargs))
            if result.get('error') or result.get('complete') is False:
                errors.append({'id': doc_id, 'code': result.get('code', 'PARTIAL_TEXT'), 'error': result.get('error', 'Incomplete typed-text extraction')})
                continue
            searched.append(doc_id)
            text = result.get('text', '')
            if not text and isinstance(result.get('pages'), list):
                text = '\n'.join(str(page.get('text', '')) for page in result['pages'] if isinstance(page, dict))
            index = text.casefold().find(query.casefold())
            if index >= 0:
                matches.append({'id': doc_id, 'snippet': text[max(0, index-100):index+len(query)+200]})
        return json.dumps({'matches': matches, 'searched_ids': searched, 'errors': errors, 'complete': not errors,
                           'scope': 'Only the explicitly supplied document IDs; typed text, not handwriting.'})
    except Exception as exc:
        return json.dumps({'error': str(exc), 'code': 'SEARCH_FAILED'})


# ── handlers ────────────────────────────────────────────────────────────────

def remarkable_enroll(args: dict, **kwargs) -> str:
    """Guide secure enrollment; one-time codes never enter model arguments."""
    if args.get("code"):
        return json.dumps({"error": "Enter the one-time code only in the local masked setup prompt, never chat.", "code": "SECURE_INPUT_REQUIRED"})
    return json.dumps({
        "setup_command": "hermes remarkable enroll",
        "standalone_command": f'"{sys.executable}" "{_PLUGIN_DIR / "cli.py"}" enroll',
        "enrolled": Path(_resolve_token_file(kwargs.get("config", _CONFIG))).is_file(),
        "instructions": "Run setup in your own terminal. It opens reMarkable account pairing and asks for the code in a masked prompt. Never paste the code in chat. You may instead configure token_file to reuse an existing device-token file.",
        "pairing_url": "https://my.remarkable.com/device/desktop/connect",
    })


def remarkable_status(args: dict, **kwargs) -> str:
    """Enrollment + cloud reachability + library size."""
    try:
        cfg = dict(kwargs.get("config", _CONFIG))
        resp = _dispatch("status", {"transport": args.get("transport")}, cfg, timeout_ms=60_000)
        if not resp.get("ok"):
            return json.dumps(_err(resp))
        return json.dumps(resp.get("result", {}))
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Status failed: {exc}", "code": "INTERNAL"})


def remarkable_list(args: dict, **kwargs) -> str:
    """List library entries (optionally filtered to one parent folder)."""
    try:
        cfg = dict(kwargs.get("config", _CONFIG))
        side_args: dict = {"refresh": bool(args.get("refresh", False)),
                           "include_tags": bool(args.get("include_tags", False))}
        if args.get("parent") is not None:
            side_args["parent"] = str(args["parent"])
        side_args["offset"] = max(0, int(args.get("offset", 0)))
        side_args["query"] = str(args.get("query", ""))
        side_args["include_trash"] = bool(args.get("include_trash", False))
        limit = args.get("limit")
        if limit is not None:
            side_args["limit"] = max(1, min(int(limit), 200))
        side_args["transport"] = args.get("transport")
        resp = _dispatch("list", side_args, cfg)
        if not resp.get("ok"):
            return json.dumps(_err(resp))
        return json.dumps(resp.get("result", {}))
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"List failed: {exc}", "code": "INTERNAL"})


def remarkable_download(args: dict, **kwargs) -> str:
    """Download an entry by id to a local path."""
    try:
        cfg = dict(kwargs.get("config", _CONFIG))
        entry_id = str(args.get("id", "")).strip()
        out_path = str(args.get("out_path", "")).strip()
        if not entry_id or not out_path:
            return json.dumps({"error": "id and out_path are required", "code": "BAD_ARGS"})
        resp = _dispatch("download", {"id": entry_id, "outPath": out_path, "transport": args.get("transport"),
                                        "format": args.get("format", "original" if (args.get('transport') or cfg.get('transport', 'cloud')) == 'cloud' else 'archive'),
                                        "overwrite": args.get("overwrite") is True}, cfg)
        if not resp.get("ok"):
            return json.dumps(_err(resp))
        return json.dumps(resp.get("result", {}))
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Download failed: {exc}", "code": "INTERNAL"})


def remarkable_upload(args: dict, **kwargs) -> str:
    """Upload a local PDF/EPUB to the cloud root (add-only)."""
    try:
        cfg = dict(kwargs.get("config", _CONFIG))
        path = str(args.get("path", "")).strip()
        name = str(args.get("name", "")).strip()
        if not path or not name:
            return json.dumps({"error": "path and name are required", "code": "BAD_ARGS"})
        if not Path(path).is_file():
            return json.dumps({"error": f"file not found: {path}", "code": "NO_FILE"})
        kind = str(args.get("kind", "")).strip().lower()
        if kind not in ("pdf", "epub"):
            kind = Path(path).suffix.lstrip(".").lower()
        if kind not in ("pdf", "epub"):
            return json.dumps({"error": f"unsupported file kind {kind!r}; pass kind='pdf' or kind='epub'", "code": "BAD_KIND"})
        if not isinstance(args.get("parent"), str):
            return json.dumps({"error": "parent must be explicit: folder id or empty string for root", "code": "BAD_ARGS"})
        resp = _dispatch(f"upload_{kind}", {"path": path, "name": name, "transport": args.get("transport"),
                                               "parent": str(args.get("parent", ""))}, cfg)
        if not resp.get("ok"):
            return json.dumps(_err(resp))
        result = resp.get("result", {})
        result["kind"] = kind
        return json.dumps(result)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Upload failed: {exc}", "code": "INTERNAL"})
