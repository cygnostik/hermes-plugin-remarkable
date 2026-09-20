"""Offline tests: loopback HTTP only; SSH uses deterministic subprocess fixtures."""
import importlib.util
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / "local_transport.py"


def load_transport():
    spec = importlib.util.spec_from_file_location("remarkable_local_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OptInTests(unittest.TestCase):
    def test_default_url_and_non_boolean_opt_in(self):
        module = load_transport()
        self.assertEqual(module._USB({}).base, "http://10.11.99.1")
        for transport in ("usb", "ssh"):
            result = module.call_local(transport, "status", {}, {transport + "_enabled": "true"})
            self.assertEqual(result["code"], "TRANSPORT_DISABLED")
        self.assertEqual(module.call_local("auto", "upload", {}, {})["code"], "UNSUPPORTED")
        self.assertEqual(module.call_local("usb", "status", None, {})["code"], "BAD_ARGS")

    def test_local_transport_is_explicitly_disabled_by_default(self):
        self.assertTrue(MODULE.exists(), "local transport implementation is missing")
        module = load_transport()
        for transport in ("usb", "ssh"):
            result = module.call_local(transport, "status", {}, {})
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "TRANSPORT_DISABLED")


import io
import json
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

# --basetemp must be set to Hermes scratch. Unlike pytest's tmp_path,
# unittest's TemporaryDirectory otherwise ignores --basetemp on Windows.
if __name__ != "__main__":
    import pytest

    @pytest.fixture(autouse=True, scope="module")
    def _stdlib_temp_under_pytest_base(tmp_path_factory):
        scratch = tmp_path_factory.mktemp("stdlib")
        with patch.object(tempfile, "tempdir", str(scratch)):
            yield

DOC = "11111111-1111-4111-8111-111111111111"
FOLDER = "22222222-2222-4222-8222-222222222222"
PAGE = "33333333-3333-4333-8333-333333333333"
NEW = "44444444-4444-4444-8444-444444444444"


class UsbTests(unittest.TestCase):
    def setUp(self):
        self.module = load_transport()
        self.requests = []
        self.routes = {
            "/documents/": (200, "application/json", json.dumps([
                {"ID": FOLDER, "VissibleName": "Folder", "Type": "CollectionType"}
            ]).encode()),
            "/documents/" + FOLDER: (200, "application/json", json.dumps([
                {"ID": DOC, "VissibleName": "Notes", "Type": "DocumentType"}
            ]).encode()),
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.requests.append(("GET", self.path, b""))
                status, content_type, data = owner.routes.get(self.path, (404, "text/plain", b"secret body"))
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                if status in (301, 302, 307, 308):
                    self.send_header("Location", "/unexpected")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append(("POST", self.path, body))
                callback = getattr(owner, "on_upload", None)
                if callback:
                    callback(body, self.headers)
                self.send_response(getattr(owner, "post_status", 200))
                self.end_headers()
                self.wfile.write(b"secret response not for output")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {"usb_enabled": True, "usb_url": f"http://127.0.0.1:{self.server.server_port}"}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_status_and_recursive_listing_use_documented_endpoints(self):
        status = self.module.call_local("usb", "status", {}, self.config)
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["result"]["transport"], "usb")
        self.assertTrue(status["result"]["reachable"])
        result = self.module.call_local("usb", "list", {}, self.config)
        self.assertTrue(result["ok"], result)
        entries = result["result"]["entries"]
        self.assertEqual([e["id"] for e in entries], [FOLDER, DOC])
        self.assertEqual(entries[1]["parent"], FOLDER)
        self.assertEqual(entries[1]["name"], "Notes")
        self.assertEqual(result["result"]["matched"], 2)
        self.assertEqual([r[1] for r in self.requests], ["/documents/", "/documents/", "/documents/" + FOLDER])

    def test_download_pdf_and_archive_validate_and_write_exact_bytes(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(DOC + ".metadata", "{}")
        for kind, data in (("pdf", b"%PDF-1.7\nfixture"), ("rmdoc", archive.getvalue())):
            with self.subTest(kind=kind):
                self.routes[f"/download/{DOC}/{kind}"] = (200, "application/octet-stream", data)
                target = Path(self.tmp.name) / ("download." + kind)
                result = self.module.call_local("usb", "download", {"id": DOC, "format": kind, "outPath": str(target)}, self.config)
                self.assertTrue(result["ok"], result)
                self.assertEqual(target.read_bytes(), data)
                self.assertEqual(result["result"]["bytes"], len(data))
                again = self.module.call_local("usb", "download", {"id": DOC, "format": kind, "out_path": str(target)}, self.config)
                self.assertEqual(again["code"], "FILE_EXISTS")

    def test_failed_sync_leaves_no_final_artifact_and_retry_succeeds(self):
        data = b"%PDF-1.7\nretry fixture"
        self.routes[f"/download/{DOC}/pdf"] = (200, "application/pdf", data)
        target = Path(self.tmp.name) / "retry.pdf"
        unrelated = target.parent / ".unrelated.tmp"
        unrelated.write_bytes(b"keep")
        args = {"id": DOC, "format": "pdf", "outPath": str(target)}
        with patch.object(self.module.os, "fsync", side_effect=OSError("synthetic disk failure")):
            failed = self.module.call_local("usb", "download", args, self.config)
        self.assertEqual(failed["code"], "LOCAL_ERROR")
        self.assertFalse(target.exists(), "Failed export must not publish a final-named artifact")
        self.assertEqual(set(target.parent.iterdir()), {unrelated})
        retried = self.module.call_local("usb", "download", args, self.config)
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(unrelated.read_bytes(), b"keep")

    def test_download_rejects_old_firmware_pdf_disguised_as_rmdoc(self):
        self.routes[f"/download/{DOC}/rmdoc"] = (200, "application/pdf", b"%PDF-1.7\nwrong")
        target = Path(self.tmp.name) / "bad.rmdoc"
        result = self.module.call_local("usb", "download", {"id": DOC, "outPath": str(target)}, self.config)
        self.assertEqual(result["code"], "BAD_RESPONSE")
        self.assertFalse(target.exists())

    def test_http_redirect_and_error_do_not_leak_response_or_follow(self):
        for status in (302, 404, 500):
            with self.subTest(status=status):
                self.routes["/documents/"] = (status, "text/plain", b"secret response")
                result = self.module.call_local("usb", "status", {}, self.config)
                self.assertEqual(result["code"], "HTTP_" + str(status))
                self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(len(self.requests), 3)

    def test_bad_uuid_is_rejected_before_network(self):
        result = self.module.call_local("usb", "download", {"id": "../../secret", "outPath": str(Path(self.tmp.name) / "bad")}, self.config)
        self.assertEqual(result["code"], "BAD_ARGS")
        self.assertEqual(self.requests, [])

    def test_pdf_upload_requires_explicit_root_and_verifies_new_entry(self):
        source = Path(self.tmp.name) / "input.pdf"
        source.write_bytes(b"%PDF-1.7\nfixture upload")
        args = {"path": str(source), "name": "My note", "parent": "", "kind": "pdf"}
        disabled = self.module.call_local("usb", "upload", args, self.config)
        self.assertEqual(disabled["code"], "WRITE_DISABLED")
        self.assertEqual(self.requests, [])
        self.config["usb_allow_upload"] = True

        def uploaded(body, headers):
            self.assertIn('multipart/form-data; boundary=', headers["Content-Type"])
            self.assertIn(b'name="file"; filename="My note.pdf"', body)
            self.assertIn(source.read_bytes(), body)
            self.routes["/documents/"] = (200, "application/json", json.dumps([
                {"ID": FOLDER, "VissibleName": "Folder", "Type": "CollectionType"},
                {"ID": NEW, "VissibleName": "My note", "Type": "DocumentType"}
            ]).encode())
        self.on_upload = uploaded
        result = self.module.call_local("usb", "upload", args, self.config)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["id"], NEW)
        self.assertTrue(result["result"]["verified"])
        self.assertEqual([(r[0], r[1]) for r in self.requests], [("GET", "/documents/"), ("POST", "/upload"), ("GET", "/documents/")])

    def test_folder_upload_is_unsupported_without_root_fallback(self):
        self.config["usb_allow_upload"] = True
        result = self.module.call_local("usb", "upload", {"parent": FOLDER}, self.config)
        self.assertEqual(result["code"], "UNSUPPORTED")
        self.assertEqual(self.requests, [])
        result = self.module.call_local("usb", "upload", {}, self.config)
        self.assertEqual(result["code"], "BAD_ARGS")
        self.assertEqual(self.requests, [])

    def test_ambiguous_upload_is_never_retried_or_claimed_verified(self):
        self.config["usb_allow_upload"] = True
        source = Path(self.tmp.name) / "input.pdf"
        source.write_bytes(b"%PDF-1.7\nfixture")
        args = {"parent": "", "path": str(source), "name": "Unverified"}
        result = self.module.call_local("usb", "upload", args, self.config)
        self.assertEqual(result["code"], "WRITE_UNVERIFIED")
        self.assertEqual(sum(r[0] == "POST" for r in self.requests), 1)
        self.assertNotIn("secret response", json.dumps(result))
        self.requests.clear()
        self.post_status = 500
        result = self.module.call_local("usb", "upload", args, self.config)
        self.assertEqual(result["code"], "WRITE_UNVERIFIED")
        self.assertEqual(sum(r[0] == "POST" for r in self.requests), 1)

    def test_upload_cannot_verify_a_mismatched_folder_response(self):
        self.config["usb_allow_upload"] = True
        source = Path(self.tmp.name) / "input.pdf"
        source.write_bytes(b"%PDF-1.7\nfixture")
        def uploaded(body, headers):
            self.routes["/documents/"] = (200, "application/json", json.dumps([
                {"ID": NEW, "VissibleName": "Race", "Type": "DocumentType", "Parent": FOLDER}
            ]).encode())
        self.on_upload = uploaded
        result = self.module.call_local("usb", "upload", {"path": str(source), "name": "Race", "parent": ""}, self.config)
        self.assertEqual(result["code"], "WRITE_UNVERIFIED")
        self.assertEqual(sum(r[0] == "POST" for r in self.requests), 1)

    def test_invalid_url_configuration_never_contacts_server(self):
        for url in ("http://user:secret@127.0.0.1", "file:///secret", "http://localhost/path", "http://localhost/?secret", "http://localhost:bad", "http://[broken"):
            with self.subTest(url=url):
                result = self.module.call_local("usb", "status", {}, dict(self.config, usb_url=url))
                self.assertEqual(result["code"], "BAD_CONFIG")
                self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(self.requests, [])

    def test_invalid_list_arguments_fail_before_io(self):
        for args in ({"parent": "../secret"}, {"limit": 0}, {"limit": True}, {"parent": None}):
            result = self.module.call_local("usb", "list", args, self.config)
            self.assertEqual(result["code"], "BAD_ARGS")
        self.assertEqual(self.requests, [])

    def test_malformed_list_and_failed_subfolder_are_not_partial_success(self):
        for payload in (b"not json secret", b'{}', b'[{"ID":"../bad"}]'):
            self.routes["/documents/"] = (200, "application/json", payload)
            result = self.module.call_local("usb", "list", {}, self.config)
            self.assertEqual(result["code"], "BAD_RESPONSE")
            self.assertNotIn("secret", json.dumps(result))
        self.routes["/documents/"] = (200, "application/json", json.dumps([{"ID": FOLDER, "Type": "CollectionType"}]).encode())
        self.routes["/documents/" + FOLDER] = (503, "text/plain", b"secret")
        self.assertEqual(self.module.call_local("usb", "list", {}, self.config)["code"], "HTTP_503")

    def test_download_size_cap_and_invalid_pdf_leave_no_output(self):
        target = Path(self.tmp.name) / "oversize.pdf"
        args = {"id": DOC, "out_path": str(target), "format": "pdf"}
        self.routes[f"/download/{DOC}/pdf"] = (200, "text/html", b"secret wrong content")
        self.assertEqual(self.module.call_local("usb", "download", args, self.config)["code"], "BAD_RESPONSE")
        with patch.object(self.module, "_MAX_FILE", 8):
            self.assertEqual(self.module.call_local("usb", "download", args, self.config)["code"], "TOO_LARGE")
        self.assertFalse(target.exists())

    def test_unsupported_usb_writes_do_not_contact_device(self):
        for op in ("move", "delete", "mkdir", "enroll", "typed_text"):
            self.assertEqual(self.module.call_local("usb", op, {}, self.config)["code"], "UNSUPPORTED")
        self.assertEqual(self.requests, [])

    def test_list_parent_is_direct_and_limit_reports_truncation(self):
        result = self.module.call_local("usb", "list", {"parent": FOLDER}, self.config)
        self.assertTrue(result["ok"], result)
        self.assertEqual([e["id"] for e in result["result"]["entries"]], [DOC])
        result = self.module.call_local("usb", "list", {"limit": 1}, self.config)
        self.assertEqual(result["result"]["matched"], 2)
        self.assertTrue(result["result"]["truncated"])


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.module = load_transport()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "output.pdf"
        self.data = b"%PDF-1.7\ncomplete bytes"

    def save(self):
        return self.module._save(self.data, self.target, "usb", "pdf", DOC)

    def test_staging_write_flush_close_failures_cleanup_and_allow_retry(self):
        factory = self.module.tempfile.NamedTemporaryFile
        for phase in ("write", "short_write", "flush", "close"):
            with self.subTest(phase=phase):
                class FaultyStream:
                    def __init__(self, **kwargs):
                        self.stream = factory(**kwargs)
                        self.name = self.stream.name
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        self.stream.close()
                        if phase == "close":
                            raise OSError("synthetic close failure")
                    def write(self, data):
                        if phase in ("write", "short_write"):
                            self.stream.write(data[:5])
                            self.stream.flush()
                            if phase == "short_write":
                                return 5
                            raise OSError("synthetic disk full after partial write")
                        return self.stream.write(data)
                    def flush(self):
                        if phase == "flush":
                            raise OSError("synthetic flush failure")
                        return self.stream.flush()
                    def fileno(self):
                        return self.stream.fileno()
                with patch.object(self.module.tempfile, "NamedTemporaryFile", FaultyStream):
                    with self.assertRaises(OSError):
                        self.save()
                self.assertEqual(list(self.root.iterdir()), [])
                result = self.save()
                self.assertEqual(self.target.read_bytes(), self.data)
                self.assertEqual(result["bytes"], len(self.data))
                self.target.unlink()

    def test_publish_occurs_only_after_sync_and_close(self):
        factory, real_sync, real_link = (self.module.tempfile.NamedTemporaryFile,
                                        self.module.os.fsync, self.module.os.link)
        streams, synced = [], []
        def staging(**kwargs):
            stream = factory(**kwargs)
            streams.append(stream)
            self.assertEqual(Path(stream.name).parent, self.target.parent)
            self.assertFalse(self.target.exists())
            return stream
        def sync(fd):
            self.assertFalse(self.target.exists())
            synced.append(fd)
            real_sync(fd)
        def publish(source, target):
            self.assertTrue(synced)
            self.assertTrue(streams[0].closed)
            self.assertEqual(Path(source).read_bytes(), self.data)
            self.assertFalse(self.target.exists())
            return real_link(source, target)
        with patch.object(self.module.tempfile, "NamedTemporaryFile", side_effect=staging), \
             patch.object(self.module.os, "fsync", side_effect=sync), \
             patch.object(self.module.os, "link", side_effect=publish):
            self.save()
        self.assertEqual(list(self.root.iterdir()), [self.target])

    def test_racing_existing_file_is_preserved_and_only_owned_stage_removed(self):
        unrelated = self.root / ".unrelated.tmp"
        unrelated.write_bytes(b"not owned")
        real_link = self.module.os.link
        def race(source, target):
            self.target.write_bytes(b"other writer won")
            return real_link(source, target)
        with patch.object(self.module.os, "link", side_effect=race):
            with self.assertRaises(self.module.LocalError) as failure:
                self.save()
        self.assertEqual(failure.exception.code, "FILE_EXISTS")
        self.assertEqual(self.target.read_bytes(), b"other writer won")
        self.assertEqual(unrelated.read_bytes(), b"not owned")
        self.assertEqual(set(self.root.iterdir()), {self.target, unrelated})

    def test_racing_symlink_is_neither_followed_nor_removed(self):
        destination = self.root / "unrelated.pdf"
        # Real OS semantics, not a mocked symlink. Unprivileged Windows sessions
        # cannot create symlinks; report that qualification gap explicitly.
        try:
            self.target.symlink_to(destination)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows symlink creation privilege unavailable (WinError 1314)")
            raise
        self.target.unlink()
        real_link = self.module.os.link
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                if not dangling:
                    destination.write_bytes(b"untouched")
                def race(source, target):
                    self.target.symlink_to(destination)
                    return real_link(source, target)
                with patch.object(self.module.os, "link", side_effect=race):
                    with self.assertRaises(self.module.LocalError) as failure:
                        self.save()
                self.assertEqual(failure.exception.code, "FILE_EXISTS")
                self.assertTrue(self.target.is_symlink())
                self.assertEqual(destination.exists(), not dangling)
                if not dangling:
                    self.assertEqual(destination.read_bytes(), b"untouched")
                    destination.unlink()
                self.assertEqual(set(self.root.iterdir()), {self.target})
                self.target.unlink()

    def test_failed_publish_does_not_fall_back_to_overwrite(self):
        with patch.object(self.module.os, "link", side_effect=OSError("hard links unavailable")):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(list(self.root.iterdir()), [])
        self.save()
        self.assertEqual(self.target.read_bytes(), self.data)


class SshTests(unittest.TestCase):
    def setUp(self):
        self.module = load_transport()
        self.config = {"ssh_enabled": True, "ssh_host": "remarkable.local"}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.remote = Path(self.tmp.name) / "remote"
        self.remote.mkdir()
        (self.remote / (DOC + ".metadata")).write_text(json.dumps({"visibleName": "Notes", "parent": FOLDER, "type": "DocumentType"}), encoding="utf-8")
        (self.remote / (FOLDER + ".metadata")).write_text(json.dumps({"visibleName": "Folder", "parent": "", "type": "CollectionType"}), encoding="utf-8")
        self.calls = []
        # The wrapper records the intended system-sftp command, then executes a
        # real child Python fixture (no SSH connection) using identical stdin.
        import subprocess
        import sys
        real_run = subprocess.run

        def run(command, **kwargs):
            self.calls.append((command, kwargs))
            self.assertEqual(command[0], "sftp")
            self.assertFalse(kwargs.get("shell", False))
            self.assertIn("BatchMode=yes", command)
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("PasswordAuthentication=no", command)
            self.assertIn("KbdInteractiveAuthentication=no", command)
            self.assertIn("PermitLocalCommand=no", command)
            self.assertIn("-b", command)
            return real_run([sys.executable, str(Path(__file__).resolve()), "--sftp-fixture", str(self.remote)], **kwargs)
        self.patcher = patch.object(subprocess, "run", side_effect=run)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_export_archive_is_local_zip_of_exact_document_files(self):
        (self.remote / (DOC + ".content")).write_text('{"pages": []}', encoding="utf-8")
        pages = self.remote / DOC
        pages.mkdir()
        (pages / (PAGE + ".rm")).write_bytes(b"reMarkable .lines file, version=6          ")
        target = Path(self.tmp.name) / "raw.rmdoc"
        result = self.module.call_local("ssh", "download", {"id": DOC, "format": "archive", "outPath": str(target)}, self.config)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["kind"], "archive")
        self.assertFalse(result["result"]["atomic_snapshot"])
        with zipfile.ZipFile(target) as z:
            self.assertEqual(set(z.namelist()), {DOC + ".metadata", DOC + ".content", DOC + "/" + PAGE + ".rm"})
            self.assertEqual(z.read(DOC + "/" + PAGE + ".rm"), (pages / (PAGE + ".rm")).read_bytes())
        batches = "\n".join(k["input"] for _, k in self.calls)
        self.assertIn("get " + FOLDER + ".metadata .", batches)
        self.assertNotIn("get -R " + FOLDER, batches)

    def test_export_rejects_inherited_trash_before_fetching_payload(self):
        (self.remote / (DOC + ".content")).write_text("{}")
        (self.remote / (DOC + ".metadata")).write_text(json.dumps({
            "parent": NEW, "type": "DocumentType"}))
        (self.remote / (NEW + ".metadata")).write_text(json.dumps({
            "parent": FOLDER, "type": "CollectionType"}))
        for marker in ({"parent": "trash"}, {"parent": "", "deleted": True}):
            for op in ("download", "export"):
                with self.subTest(marker=marker, op=op):
                    (self.remote / (FOLDER + ".metadata")).write_text(json.dumps({
                        "type": "CollectionType", **marker}))
                    self.calls.clear()
                    target = Path(self.tmp.name) / f"trashed-{op}-{bool(marker.get('deleted'))}.zip"
                    result = self.module.call_local("ssh", op, {
                        "id": DOC, "outPath": str(target), "include_deleted": True,
                        "include_trash": True}, self.config)
                    self.assertEqual(result.get("code"), "NOT_FOUND", result)
                    self.assertFalse(target.exists())
                    batches = "\n".join(k["input"] for _, k in self.calls)
                    self.assertNotIn("get -R", batches)
                    self.assertNotIn(DOC + ".content", batches)

    def test_export_missing_document_does_not_create_file(self):
        target = Path(self.tmp.name) / "missing.zip"
        result = self.module.call_local("ssh", "export", {"id": NEW, "outPath": str(target)}, self.config)
        self.assertEqual(result["code"], "NOT_FOUND")
        self.assertFalse(target.exists())

    def test_ssh_never_supports_remote_writes_or_rendered_pdf(self):
        for op, args in (("upload", {}), ("delete", {}), ("move", {}), ("typed_text", {}), ("download", {"id": DOC, "format": "pdf"})):
            with self.subTest(op=op):
                result = self.module.call_local("ssh", op, args, self.config)
                self.assertEqual(result["code"], "UNSUPPORTED")
        self.assertEqual(self.calls, [])

    def test_invalid_ssh_inputs_never_reach_subprocess(self):
        cases = [("ssh_host", "-oProxyCommand=evil"), ("ssh_host", "host;evil"),
                 ("ssh_host", "host\ncommand"), ("ssh_host", "user@host"),
                 ("ssh_host", "host:/secret"), ("ssh_user", "root\n!evil"),
                 ("ssh_port", "22 -x"), ("ssh_identity", "file\n!evil")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                result = self.module.call_local("ssh", "status", {}, dict(self.config, **{key: value}))
                self.assertEqual(result["code"], "BAD_CONFIG")
        for entry_id in ("../../etc/passwd", DOC + "\n!evil", "*", "-rf"):
            result = self.module.call_local("ssh", "download", {"id": entry_id, "outPath": "unused"}, self.config)
            self.assertEqual(result["code"], "BAD_ARGS")
        self.assertEqual(self.calls, [])

    def test_errors_never_echo_ssh_stderr_or_attempt_password(self):
        import subprocess
        for failure, code in ((FileNotFoundError("secret key"), "NO_SFTP"),
                              (subprocess.TimeoutExpired("secret", 1), "TIMEOUT")):
            with patch.object(subprocess, "run", side_effect=failure):
                result = self.module.call_local("ssh", "status", {}, self.config)
                self.assertEqual(result["code"], code)
                self.assertNotIn("secret", json.dumps(result))
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "", "secret password")):
            result = self.module.call_local("ssh", "status", {}, self.config)
            self.assertEqual(result["code"], "SSH_FAILED")
            self.assertNotIn("secret", json.dumps(result))

    def test_identity_port_and_empty_library(self):
        for p in self.remote.iterdir():
            p.unlink()
        identity = Path(self.tmp.name) / "identity fixture"
        identity.write_text("not a key; child fixture does not authenticate")
        self.config.update({"ssh_identity": str(identity), "ssh_port": 2222})
        result = self.module.call_local("ssh", "list", {}, self.config)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["entries"], [])
        command = self.calls[0][0]
        self.assertIn(str(identity), command)
        self.assertIn("2222", command)
        self.assertIn("IdentitiesOnly=yes", command)

    def test_invalid_ssh_list_arguments_fail_before_subprocess(self):
        for args in ({"parent": "../secret"}, {"limit": 0}, {"limit": True}):
            self.assertEqual(self.module.call_local("ssh", "list", args, self.config)["code"], "BAD_ARGS")
        self.assertEqual(self.calls, [])

    def test_listing_omits_all_descendants_of_trashed_or_deleted_folders(self):
        for marker in ({"parent": "trash"}, {"parent": "", "deleted": True}):
            with self.subTest(marker=marker):
                (self.remote / (FOLDER + ".metadata")).write_text(json.dumps({
                    "type": "CollectionType", **marker}))
                (self.remote / (NEW + ".metadata")).write_text(json.dumps({
                    "type": "CollectionType", "parent": FOLDER}))
                (self.remote / (DOC + ".metadata")).write_text(json.dumps({
                    "type": "DocumentType", "parent": NEW}))
                for args in ({}, {"parent": NEW}, {"parent": FOLDER}, {"limit": 1}):
                    result = self.module.call_local("ssh", "list", args, self.config)
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["result"]["entries"], [])
                    self.assertEqual(result["result"]["matched"], 0)
                    self.assertFalse(result["result"]["truncated"])

    def test_invalid_hierarchy_is_not_silently_listed_as_active(self):
        cases = [
            ({"parent": DOC, "type": "CollectionType"}, {"parent": FOLDER, "type": "CollectionType"}),
            ({"parent": "", "type": "CollectionType"}, {"parent": NEW, "type": "DocumentType"}),
            ({"parent": "", "type": "DocumentType"}, {"parent": FOLDER, "type": "DocumentType"}),
            ({"parent": "", "type": "CollectionType"}, {"parent": "../../secret", "type": "DocumentType"}),
            ({"parent": "", "type": "CollectionType"}, {"parent": None, "type": "DocumentType"}),
            ({"parent": FOLDER, "type": "CollectionType"}, {"parent": FOLDER, "type": "DocumentType"}),
        ]
        for folder, document in cases:
            with self.subTest(folder=folder, document=document):
                for entry_id, metadata in ((FOLDER, folder), (DOC, document)):
                    (self.remote / (entry_id + ".metadata")).write_text(json.dumps(metadata))
                result = self.module.call_local("ssh", "list", {}, self.config)
                self.assertEqual(result.get("code"), "BAD_RESPONSE", result)
                self.assertNotIn("secret", json.dumps(result))
                self.assertTrue(all("secret" not in kw["input"] for _, kw in self.calls))
                (self.remote / (DOC + ".content")).write_text("{}")
                self.calls.clear()
                target = Path(self.tmp.name) / "invalid-hierarchy.zip"
                result = self.module.call_local("ssh", "export", {
                    "id": DOC, "outPath": str(target)}, self.config)
                self.assertEqual(result.get("code"), "BAD_RESPONSE", result)
                self.assertFalse(target.exists())
                batches = "\n".join(kw["input"] for _, kw in self.calls)
                self.assertNotIn("get -R", batches)
                self.assertNotIn("secret", batches)
                self.assertNotIn("get " + NEW + ".metadata", batches)

    def test_failed_archive_sync_allows_clean_retry(self):
        (self.remote / (DOC + ".content")).write_text("{}")
        target = Path(self.tmp.name) / "retry.zip"
        args = {"id": DOC, "outPath": str(target)}
        with patch.object(self.module.os, "fsync", side_effect=OSError("synthetic disk error")):
            result = self.module.call_local("ssh", "export", args, self.config)
        self.assertEqual(result["code"], "LOCAL_ERROR")
        self.assertFalse(target.exists())
        self.assertEqual(set(Path(self.tmp.name).iterdir()), {self.remote})
        result = self.module.call_local("ssh", "export", args, self.config)
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(set(archive.namelist()), {DOC + ".metadata", DOC + ".content"})
            self.assertEqual(archive.read(DOC + ".content"), b"{}")

    def test_metadata_failures_and_deleted_entries_are_explicit(self):
        (self.remote / (DOC + ".metadata")).write_text("secret malformed", encoding="utf-8")
        result = self.module.call_local("ssh", "list", {}, self.config)
        self.assertEqual(result["code"], "BAD_RESPONSE")
        self.assertNotIn("secret", json.dumps(result))
        (self.remote / (DOC + ".metadata")).write_text('{"deleted": true}', encoding="utf-8")
        result = self.module.call_local("ssh", "list", {}, self.config)
        self.assertEqual([e["id"] for e in result["result"]["entries"]], [FOLDER])

    def test_export_folder_or_oversized_content_fails_without_output(self):
        (self.remote / (FOLDER + ".content")).write_text("{}")
        target = Path(self.tmp.name) / "bad.zip"
        result = self.module.call_local("ssh", "export", {"id": FOLDER, "outPath": str(target)}, self.config)
        self.assertEqual(result["code"], "UNSUPPORTED")
        (self.remote / (DOC + ".content")).write_text("{}")
        with patch.object(self.module, "_MAX_FILE", 8):
            result = self.module.call_local("ssh", "export", {"id": DOC, "outPath": str(target)}, self.config)
            self.assertEqual(result["code"], "TOO_LARGE")
        self.assertFalse(target.exists())

    def test_status_and_list_read_fixed_xochitl_via_noninteractive_sftp(self):
        status = self.module.call_local("ssh", "status", {}, self.config)
        self.assertTrue(status["ok"], status)
        self.assertTrue(status["result"]["read_only"])
        result = self.module.call_local("ssh", "list", {}, self.config)
        self.assertTrue(result["ok"], result)
        entries = {e["id"]: e for e in result["result"]["entries"]}
        self.assertEqual(set(entries), {DOC, FOLDER})
        self.assertEqual(entries[DOC]["name"], "Notes")
        self.assertEqual(entries[DOC]["parent"], FOLDER)
        root = self.module.call_local("ssh", "list", {"parent": ""}, self.config)
        self.assertEqual([e["id"] for e in root["result"]["entries"]], [FOLDER])
        for command, kwargs in self.calls:
            self.assertEqual(command[-1], "root@remarkable.local")
            self.assertIn("/home/root/.local/share/remarkable/xochitl", kwargs["input"])
            self.assertNotIn("put ", kwargs["input"])
            self.assertNotIn("rm ", kwargs["input"])


def sftp_fixture(remote):
    """Strict offline command interpreter in a real subprocess, not an SSH server."""
    import glob
    import shlex
    import shutil
    import sys
    remote = Path(remote)
    for line in sys.stdin.read().splitlines():
        words = shlex.split(line)
        if not words:
            continue
        command, rest = words[0], words[1:]
        if command == "cd":
            assert rest == ["/home/root/.local/share/remarkable/xochitl"]
        elif command == "pwd":
            print("Remote working directory: /home/root/.local/share/remarkable/xochitl")
        elif command == "ls":
            assert rest == ["-1"]
            print("\n".join(sorted(p.name for p in remote.iterdir())))
        elif command == "get":
            if rest[0] == "-R":
                rest.pop(0)
            source, dest = rest
            sources = glob.glob(str(remote / source))
            if not sources:
                return 1
            for item in sources:
                item = Path(item)
                target = Path(dest) / item.name if Path(dest).is_dir() else Path(dest)
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copyfile(item, target)
        elif command in ("bye", "quit"):
            return 0
        else:
            raise AssertionError("Unexpected SFTP command")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--sftp-fixture":
        raise SystemExit(sftp_fixture(sys.argv[2]))
    unittest.main()
