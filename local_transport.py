"""Explicit opt-in local transports. No cloud fallback or device configuration changes."""
from __future__ import annotations


import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import uuid
import zipfile
import urllib.error
import urllib.parse
import urllib.request

_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_MAX_JSON = 8 * 1024 * 1024
_MAX_ENTRIES = 10000
_MAX_FILE = 256 * 1024 * 1024
_USB_LOCK = threading.RLock()
_REMOTE_ROOT = "/home/root/.local/share/remarkable/xochitl"


class LocalError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _uuid(value):
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise LocalError("BAD_ARGS", "A canonical UUID is required")
    return value


def _timeout(config):
    value = config.get("timeout_ms", 60000)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 100 <= value <= 600000:
        raise LocalError("BAD_CONFIG", "timeout_ms must be between 100 and 600000")
    return value / 1000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _USB:
    def __init__(self, config):
        self.base = config.get("usb_url", "http://10.11.99.1")
        if not isinstance(self.base, str) or any(c.isspace() for c in self.base):
            raise LocalError("BAD_CONFIG", "usb_url must be an HTTP origin")
        try:
            parsed = urllib.parse.urlsplit(self.base)
        except ValueError:
            raise LocalError("BAD_CONFIG", "Invalid USB origin") from None
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise LocalError("BAD_CONFIG", "usb_url must be an HTTP origin without credentials or a path")
        try:
            parsed.port
        except ValueError:
            raise LocalError("BAD_CONFIG", "Invalid USB port") from None
        self.base = self.base.rstrip("/")
        self.timeout = _timeout(config)
        # Ignore environment proxies: local tablet bytes must not leave via a proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, path, data=None, headers=None, cap=_MAX_JSON):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        with self.opener.open(req, timeout=self.timeout) as response:
            body = response.read(cap + 1)
            if len(body) > cap:
                raise LocalError("TOO_LARGE", "USB response exceeds size limit")
            return body

    def folder(self, parent):
        if parent:
            _uuid(parent)
        try:
            records = json.loads(self.request("/documents/" + parent))
        except (ValueError, UnicodeError):
            raise LocalError("BAD_RESPONSE", "USB document listing is not JSON") from None
        if not isinstance(records, list) or len(records) > _MAX_ENTRIES:
            raise LocalError("BAD_RESPONSE", "USB listing is not a bounded array")
        entries = []
        for record in records:
            if not isinstance(record, dict) or not _UUID.fullmatch(str(record.get("ID", ""))):
                raise LocalError("BAD_RESPONSE", "USB listing contains an invalid document ID")
            if record.get("Parent", parent) != parent:
                raise LocalError("BAD_RESPONSE", "USB listing returned a mismatched parent folder")
            if record.get("Deleted", record.get("deleted", False)):
                continue
            entries.append({"id": record["ID"], "name": record.get("VissibleName", record.get("VisibleName", "")),
                            "type": "folder" if record.get("Type") == "CollectionType" else "file",
                            "parent": parent, "pinned": bool(record.get("Bookmarked", False))})
        return entries


def _list_args(args):
    limit = args.get("limit", 500)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise LocalError("BAD_ARGS", "limit must be an integer between 1 and 10000")
    if "parent" in args and args["parent"] != "":
        _uuid(args["parent"])
    return limit


def _listing(entries, args, transport):
    limit = _list_args(args)
    return {"transport": transport, "entries": entries[:limit], "matched": len(entries),
            "truncated": len(entries) > limit, "cached": False}


def _output(args):
    value = args.get("outPath", args.get("out_path"))
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise LocalError("BAD_ARGS", "A local outPath or out_path is required")
    path = Path(value).expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise LocalError("FILE_EXISTS", "Destination exists; choose a new output path")
    if not path.parent.is_dir():
        raise LocalError("BAD_ARGS", "Output parent directory must already exist")
    return path


def _save(data, target, transport, kind, entry_id):
    # Stage on the destination filesystem. A hard link publishes the closed,
    # synced inode atomically and fails if *any* destination entry already
    # exists (including dangling symlinks). Never fall back to replacing it.
    staging = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".remarkable-", suffix=".tmp",
                                         dir=target.parent, delete=False) as stream:
            staging = Path(stream.name)
            if stream.write(data) != len(data):
                raise OSError("Incomplete staging write")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(staging, target)
        except FileExistsError:
            raise LocalError("FILE_EXISTS", "Destination exists; choose a new output path") from None
    finally:
        # Only the unique staging file belongs to this operation. In particular,
        # never unlink target: another writer may have won the publication race.
        if staging is not None:
            staging.unlink()
    return {"transport": transport, "id": entry_id, "outPath": str(target), "kind": kind, "bytes": len(data)}


def _validate_download(data, kind):
    if kind == "pdf":
        valid = data.startswith(b"%PDF-")
    else:
        valid = zipfile.is_zipfile(io.BytesIO(data))
    if not valid:
        raise LocalError("BAD_RESPONSE", "Downloaded bytes do not match requested format; check firmware support")


def _usb_upload(args, config):
    if config.get("usb_allow_upload") is not True:
        raise LocalError("WRITE_DISABLED", "USB uploads require usb_allow_upload=true")
    if "parent" not in args:
        raise LocalError("BAD_ARGS", "USB root upload requires explicit parent='' acknowledgement")
    if args["parent"] != "":
        raise LocalError("UNSUPPORTED", "USB has no atomic folder-targeted upload; refusing root fallback")
    if args.get("kind", "pdf") != "pdf":
        raise LocalError("UNSUPPORTED", "USB upload supports PDF only")
    value, name = args.get("path"), args.get("name")
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise LocalError("BAD_ARGS", "A local PDF path is required")
    if (not isinstance(name, str) or not name or len(name) > 160
            or any(ord(c) < 32 or c in '/\\"<>:|?*' for c in name)):
        raise LocalError("BAD_ARGS", "A safe document name is required")
    source = Path(value).expanduser()
    if not source.is_file():
        raise LocalError("NO_FILE", "Local PDF file not found")
    with source.open("rb") as stream:
        data = stream.read(_MAX_FILE + 1)
    if len(data) > _MAX_FILE:
        raise LocalError("TOO_LARGE", "PDF exceeds upload limit")
    if not data.startswith(b"%PDF-"):
        raise LocalError("BAD_ARGS", "Upload must contain PDF bytes")
    display_name = name[:-4] if name.lower().endswith(".pdf") else name
    filename = display_name + ".pdf"
    client = _USB(config)
    before = {e["id"] for e in client.folder("")}
    boundary = "hermes-remarkable-" + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            'Content-Type: application/pdf\r\n\r\n').encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode()
    try:
        client.request("/upload", data=body, headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Origin": client.base, "Referer": client.base + "/"})
        candidates = [e for e in client.folder("") if e["id"] not in before
                      and e["name"] in (display_name, filename) and e["type"] == "file"]
        if len(candidates) != 1:
            raise LocalError("WRITE_UNVERIFIED", "Upload not uniquely visible")
    except Exception:
        raise LocalError("WRITE_UNVERIFIED", "USB upload may have succeeded; inspect the tablet before retrying. No automatic retry or fallback.") from None
    return {"transport": "usb", "id": candidates[0]["id"], "name": candidates[0]["name"],
            "parent": "", "kind": "pdf", "bytes": len(data), "verified": True,
            "verification": "new root listing entry; not content equality",
            "warning": "USB upload destination is shared tablet state; do not use another USB web client concurrently"}


def _usb(op, args, config):
    if op not in ("status", "list", "download", "export", "upload"):
        raise LocalError("UNSUPPORTED", "Operation is not supported by USB")
    if op == "upload":
        return _usb_upload(args, config)
    if op in ("download", "export"):
        entry_id = _uuid(args.get("id"))
        kind = args.get("format", "rmdoc")
        if kind == "archive":
            kind = "rmdoc"
        if kind not in ("pdf", "rmdoc"):
            raise LocalError("UNSUPPORTED", "USB export supports only PDF and rmdoc")
        target = _output(args)
        data = _USB(config).request(f"/download/{entry_id}/{kind}", cap=_MAX_FILE)
        _validate_download(data, kind)
        return _save(data, target, "usb", kind, entry_id)
    client = _USB(config)
    if op == "status":
        client.folder("")
        return {"transport": "usb", "reachable": True, "read_only": config.get("usb_allow_upload") is not True}
    parent = args.get("parent", "")
    if not isinstance(parent, str):
        raise LocalError("BAD_ARGS", "parent must be a folder UUID or empty root string")
    pending, seen, entries = [parent], set(), {}
    while pending:
        folder = pending.pop(0)
        if folder in seen:
            continue
        seen.add(folder)
        for entry in client.folder(folder):
            entries.setdefault(entry["id"], entry)
            if len(entries) > _MAX_ENTRIES:
                raise LocalError("TOO_LARGE", "USB library exceeds entry limit")
            if "parent" not in args and entry["type"] == "folder" and entry["id"] not in seen:
                pending.append(entry["id"])
    return _listing(list(entries.values()), args, "usb")


class _SSH:
    def __init__(self, config):
        host = config.get("ssh_host")
        user = config.get("ssh_user", "root")
        if (not isinstance(host, str) or len(host) > 253
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)
                or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in host.split("."))):
            raise LocalError("BAD_CONFIG", "ssh_host must be an explicit IPv4 address or DNS hostname")
        if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}", user):
            raise LocalError("BAD_CONFIG", "Invalid ssh_user")
        port = config.get("ssh_port", 22)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise LocalError("BAD_CONFIG", "ssh_port must be an integer between 1 and 65535")
        self.timeout = _timeout(config)
        self.command = ["sftp", "-F", os.devnull, "-b", "-", "-P", str(port)]
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "PasswordAuthentication=no",
                       "KbdInteractiveAuthentication=no", "PreferredAuthentications=publickey",
                       "PermitLocalCommand=no", "ProxyCommand=none", "ProxyJump=none",
                       "ClearAllForwardings=yes", "ForwardAgent=no", "ForwardX11=no",
                       "UpdateHostKeys=no", "ControlPath=none", "ConnectionAttempts=1", "ConnectTimeout=10"):
            self.command.extend(["-o", option])
        identity = config.get("ssh_identity")
        if identity is not None:
            if not isinstance(identity, str) or not identity or any(ord(c) < 32 for c in identity):
                raise LocalError("BAD_CONFIG", "ssh_identity must be a local identity file path")
            path = Path(identity).expanduser().absolute()
            if not path.is_file():
                raise LocalError("BAD_CONFIG", "SSH identity file is unavailable")
            self.command.extend(["-o", "IdentitiesOnly=yes", "-i", str(path)])
        self.command.append(user + "@" + host)

    def run(self, commands, cwd):
        batch = "cd " + _REMOTE_ROOT + "\n" + "\n".join(commands) + "\nbye\n"
        try:
            result = subprocess.run(self.command, input=batch, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace",
                                    timeout=self.timeout, cwd=str(cwd), shell=False)
        except FileNotFoundError:
            raise LocalError("NO_SFTP", "System OpenSSH sftp executable not found") from None
        except subprocess.TimeoutExpired:
            raise LocalError("TIMEOUT", "SSH/SFTP timed out; no password prompt or fallback attempted") from None
        if result.returncode:
            raise LocalError("SSH_FAILED", "SSH/SFTP failed; verify existing host trust, noninteractive key access, and SFTP support manually")
        if len(result.stdout) > _MAX_JSON:
            raise LocalError("TOO_LARGE", "SSH listing exceeds size limit")
        return result.stdout

    def names(self, cwd):
        output = self.run(["ls -1"], cwd)
        # OpenSSH echoes batch commands. Only canonical UUID-based basenames
        # can ever become a get operand; other output is never executable data.
        names = []
        for line in output.splitlines():
            name = line.strip()
            stem = name.split(".", 1)[0]
            if _UUID.fullmatch(stem) and re.fullmatch(r"[A-Za-z0-9.-]+", name):
                names.append(name)
        if len(names) > _MAX_ENTRIES:
            raise LocalError("TOO_LARGE", "SSH library exceeds entry limit")
        return sorted(set(names))


def _metadata(path):
    if path.is_symlink() or path.stat().st_size > _MAX_JSON:
        raise LocalError("BAD_RESPONSE", "Unsafe or oversized metadata file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, UnicodeError):
        raise LocalError("BAD_RESPONSE", "Device metadata is not a JSON object") from None


def _ssh_metadata(client, root, names):
    names = [n for n in names if n.endswith(".metadata") and _UUID.fullmatch(n[:-9])]
    if names:
        client.run(["get " + n + " ." for n in names], root)
    metadata = {name[:-9]: _metadata(root / name) for name in names}
    parents = {}
    for entry_id, md in metadata.items():
        parent = md.get("parent", "")
        if not isinstance(parent, str) or (parent not in ("", "trash") and not _UUID.fullmatch(parent)):
            raise LocalError("BAD_RESPONSE", "Device metadata has an invalid parent")
        if parent not in ("", "trash"):
            if parent not in metadata or metadata[parent].get("type") != "CollectionType":
                raise LocalError("BAD_RESPONSE", "Device hierarchy has a missing or non-folder parent")
        parents[entry_id] = parent
    # Iterative memoized ancestry avoids recursion limits on deep libraries.
    deleted = {"": False, "trash": True}
    for entry_id in metadata:
        chain, visiting = [], set()
        current = entry_id
        while current not in deleted:
            if current in visiting:
                raise LocalError("BAD_RESPONSE", "Device hierarchy contains a cycle")
            visiting.add(current)
            chain.append(current)
            current = parents[current]
        for current in reversed(chain):
            deleted[current] = bool(metadata[current].get("deleted")) or deleted[parents[current]]
    return metadata, deleted


def _ssh_export(client, root, args):
    entry_id = _uuid(args.get("id"))
    target = _output(args)
    all_names = client.names(root)
    names = [n for n in all_names if n == entry_id or n.startswith(entry_id + ".")]
    if entry_id + ".metadata" not in names:
        raise LocalError("NOT_FOUND", "Document metadata not present on device")
    if entry_id + ".content" not in names or entry_id + ".tombstone" in names:
        raise LocalError("NOT_FOUND", "Document is deleted or lacks local content")
    # Fetch only validated metadata basenames to check the entire hierarchy
    # before any recursive payload transfer. Never turn a parent value into a
    # remote operand. Keep other documents' metadata outside the archive root.
    metadata_root = root / "metadata"
    metadata_root.mkdir()
    metadata, deleted = _ssh_metadata(client, metadata_root, all_names)
    md = metadata[entry_id]
    if deleted[entry_id]:
        raise LocalError("NOT_FOUND", "Document is deleted or in trash")
    if md.get("type") != "DocumentType":
        raise LocalError("UNSUPPORTED", "SSH archive export accepts a document, not a folder")
    root = root / "document"
    root.mkdir()
    metadata_name = entry_id + ".metadata"
    (metadata_root / metadata_name).rename(root / metadata_name)
    # Names have already passed the UUID-basename whitelist; never use caller
    # supplied remote paths, shell commands, or wildcards.
    client.run(["get -R " + name + " ." for name in names if name != metadata_name], root)
    data = io.BytesIO()
    size, count = 0, 0
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise LocalError("BAD_RESPONSE", "Symlinks are not allowed in document archives")
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in (".", "..") for part in relative.parts):
                raise LocalError("BAD_RESPONSE", "Unsafe document archive member")
            if relative.parts[0] not in names:
                raise LocalError("BAD_RESPONSE", "Unexpected document archive member")
            size += path.stat().st_size
            count += 1
            if size > _MAX_FILE or count > _MAX_ENTRIES:
                raise LocalError("TOO_LARGE", "Device archive exceeds export limits")
            archive.write(path, relative.as_posix())
    result = _save(data.getvalue(), target, "ssh", "archive", entry_id)
    result.update({"atomic_snapshot": False, "files": count,
                   "warning": "Raw xochitl file snapshot, not a rendered PDF or guaranteed importable rmdoc. Do not edit the document during export."})
    return result


def _ssh(op, args, config):
    if op not in ("status", "list", "download", "export"):
        raise LocalError("UNSUPPORTED", "SSH is read-only; this operation is not supported")
    if op in ("download", "export"):
        if args.get("format", "archive") not in ("archive", "rmdoc"):
            raise LocalError("UNSUPPORTED", "SSH exports raw archives only; use the local renderer for PDF or typed text")
        _uuid(args.get("id"))
    client = _SSH(config)
    with tempfile.TemporaryDirectory(prefix="remarkable-local-") as tmp:
        root = Path(tmp)
        if op in ("download", "export"):
            return _ssh_export(client, root, args)
        if op == "status":
            client.run(["pwd"], root)
            return {"transport": "ssh", "reachable": True, "read_only": True}
        metadata, deleted = _ssh_metadata(client, root, client.names(root))
        entries = []
        for entry_id, md in metadata.items():
            if deleted[entry_id]:
                continue
            entries.append({"id": entry_id, "name": md.get("visibleName", ""),
                            "type": "folder" if md.get("type") == "CollectionType" else "file",
                            "parent": md.get("parent", ""), "pinned": bool(md.get("pinned"))})
        if "parent" in args:
            if args["parent"] != "":
                _uuid(args["parent"])
            entries = [e for e in entries if e["parent"] == args["parent"]]
        return _listing(entries, args, "ssh")


def call_local(transport: str, op: str, args: dict, config: dict) -> dict:
    """Return a sidecar-shaped result; selecting this API never changes transport."""
    try:
        if not isinstance(args, dict) or not isinstance(config, dict):
            raise LocalError("BAD_ARGS", "args and config must be objects")
        if transport not in ("usb", "ssh"):
            raise LocalError("UNSUPPORTED", "Unknown local transport")
        if config.get(transport + "_enabled") is not True:
            raise LocalError("TRANSPORT_DISABLED", "Local transport requires explicit opt-in")
        if op == "list":
            _list_args(args)
        if transport == "usb":
            # Lists change the tablet's shared upload-folder state; serialize our
            # own operations, but another application cannot be locked out.
            with _USB_LOCK:
                return {"ok": True, "result": _usb(op, args, config)}
        return {"ok": True, "result": _ssh(op, args, config)}
    except LocalError as exc:
        return {"ok": False, "error": str(exc), "code": exc.code}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "USB endpoint returned HTTP " + str(exc.code), "code": "HTTP_" + str(exc.code)}
    except (TimeoutError, socket.timeout):
        return {"ok": False, "error": "Local transport timed out", "code": "TIMEOUT"}
    except urllib.error.URLError:
        return {"ok": False, "error": "USB endpoint unavailable; check cable and manually enabled USB web interface", "code": "UNREACHABLE"}
    except Exception:
        # Never echo remote response bodies, stderr, credentials, or raw config.
        return {"ok": False, "error": "Local transport failed", "code": "LOCAL_ERROR"}
