import importlib.util
import io
import os
import pathlib
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("installer", ROOT / "haos_truenas_installer.py")
installer = importlib.util.module_from_spec(SPEC)
sys.modules["installer"] = installer
SPEC.loader.exec_module(installer)

class MidcltParsingTests(unittest.TestCase):
    def client(self):
        with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/midclt"):
            return installer.MidcltClient()

    def test_local_midclt_remains_selected_when_remote_ca_env_is_present(self):
        args = installer.parse_args(["--truenas-url", "http://127.0.0.1", "--username", "", "--password", "", "--api-key", "", "--tls-ca-cert", "/tmp/truenas-ca.pem"])
        with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/midclt"):
            self.assertTrue(installer.use_local_midclt(args))

    def test_accepts_bare_system_version_scalar(self):
        proc = installer.subprocess.CompletedProcess([], 0, stdout="TrueNAS-26.0.0-BETA.2", stderr="")
        with mock.patch.object(installer.subprocess, "run", return_value=proc):
            self.assertEqual(self.client().call("system.version"), "TrueNAS-26.0.0-BETA.2")

    def test_rejects_non_json_from_other_methods_cleanly(self):
        proc = installer.subprocess.CompletedProcess([], 0, stdout="not-json", stderr="")
        with mock.patch.object(installer.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(installer.InstallerError, "invalid JSON output"):
                self.client().call("system.info")


class ManifestPathTests(unittest.TestCase):
    def test_default_manifest_uses_writable_tmp_location(self):
        path = installer.resolve_manifest_path(None, "haos-test")
        self.assertEqual(path, pathlib.Path("/tmp/haos-installer-haos-test-manifest.json"))

    def test_explicit_manifest_path_is_respected(self):
        with tempfile.TemporaryDirectory() as d:
            requested = pathlib.Path(d) / "persistent" / "manifest.json"
            self.assertEqual(installer.resolve_manifest_path(str(requested), "haos-test"), requested.resolve())

    def test_resume_manifest_rejects_untrusted_parent_directory(self):
        with tempfile.TemporaryDirectory() as d:
            shared = pathlib.Path(d) / "shared"
            shared.mkdir()
            shared.chmod(0o777)
            manifest = shared / "manifest.json"
            manifest.write_text("{}")
            manifest.chmod(0o600)
            with self.assertRaisesRegex(installer.InstallerError, "group/world-writable directory"):
                installer.read_manifest_file(manifest)

    def test_create_manifest_refuses_untrusted_parent_before_writing(self):
        plan = installer.Plan(True, "version", "tank", "tank/haos-vms/haos", "haos", "br0", 2, 4096, 64, "https://example.test/i", "a"*64, 10, False)
        with tempfile.TemporaryDirectory() as d:
            shared = pathlib.Path(d) / "shared"
            shared.mkdir()
            shared.chmod(0o777)
            manifest = shared / "manifest.json"
            with self.assertRaisesRegex(installer.InstallerError, "group/world-writable directory"):
                installer.create_manifest(manifest, plan)
            self.assertFalse(manifest.exists())

    def test_resume_manifest_rejects_foreign_owner(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = pathlib.Path(d) / "manifest.json"
            manifest.write_text("{}")
            foreign = mock.Mock(st_mode=installer.stat.S_IFREG | 0o600, st_size=2, st_uid=installer.os.geteuid() + 1)
            with mock.patch.object(installer.os, "fstat", return_value=foreign):
                with self.assertRaisesRegex(installer.InstallerError, "owned by root or the current user"):
                    installer.read_manifest_file(manifest)

    def test_resume_manifest_fifo_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = pathlib.Path(d) / "manifest.fifo"
            os.mkfifo(manifest)
            with self.assertRaisesRegex(installer.InstallerError, "regular file"):
                installer.read_manifest_file(manifest)

    def test_resume_manifest_reader_rejects_symlinks_and_group_world_writable_files(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d); target = base / "manifest.json"; target.write_text("{}")
            link = base / "manifest-link.json"; link.symlink_to(target)
            with self.assertRaises(OSError):
                installer.read_manifest_file(link)
            target.chmod(0o666)
            with self.assertRaisesRegex(installer.InstallerError, "not group/world-writable"):
                installer.read_manifest_file(target)
            oversized = base / "oversized.json"
            oversized.write_bytes(b" " * (1024 * 1024 + 1)); oversized.chmod(0o600)
            with self.assertRaisesRegex(installer.InstallerError, "1 MiB safety limit"):
                installer.read_manifest_file(oversized)

    def test_manifest_permission_error_is_actionable_and_aborts_before_download(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haostest", "haostest", "br0", 2, 2048, 32, "https://example.test/i", "a"*64, 10, False)
        args = installer.parse_args(["--vm-name", "haos-test"])
        with mock.patch.object(installer.os, "geteuid", return_value=0):
            with mock.patch.object(installer, "require_tools_for_install"):
                with mock.patch.object(installer.os, "open", side_effect=PermissionError):
                    with mock.patch.object(installer, "download_and_verify") as download:
                        with self.assertRaisesRegex(installer.InstallerError, "writable location"):
                            installer.install(object(), plan, args)
                        download.assert_not_called()

    def test_non_root_live_install_aborts_before_manifest_or_download(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haostest", "haostest", "br0", 2, 2048, 32, "https://example.test/i", "a"*64, 10, False)
        args = installer.parse_args(["--vm-name", "haos-test"])
        with mock.patch.object(installer.os, "geteuid", return_value=1000):
            with mock.patch.object(installer, "download_and_verify") as download:
                with self.assertRaisesRegex(installer.InstallerError, "requires a root TrueNAS shell"):
                    installer.install(object(), plan, args)
                download.assert_not_called()
    def test_conversion_error_points_to_manifest_resume_flow(self):
        with mock.patch.object(installer, "run_checked", side_effect=installer.subprocess.CalledProcessError(1, ["qemu-img"])):
            with self.assertRaisesRegex(installer.InstallerError, "--resume-manifest"):
                installer.convert_qcow_to_zvol(pathlib.Path("image.qcow2"), "/dev/zvol/tank/haos", pathlib.Path("/tmp/manifest.json"))


class LauncherTests(unittest.TestCase):
    def test_launcher_passes_interactive_input_from_controlling_tty(self):
        text = (ROOT / "install.sh").read_text()
        self.assertIn("if ( : </dev/tty ) 2>/dev/null; then", text)
        self.assertIn('python3 "$SRC/haos_truenas_installer.py" "$@" </dev/tty', text)
        self.assertIn('python3 "$SRC/haos_truenas_installer.py" "$@"\nfi', text)

    def test_launcher_uses_private_temp_and_does_not_use_unchecked_tar_extract(self):
        text = (ROOT / "install.sh").read_text()
        self.assertIn("umask 077", text)
        self.assertIn('mktemp -d "$TMPBASE/haos-truenas-installer.XXXXXX"', text)
        self.assertNotIn("tar -xzf", text)
        self.assertIn("--proto-redir '=https'", text)
        self.assertIn("limit = 128 * 1024 * 1024", text)
        self.assertIn("refusing an HTTPS-to-HTTP redirect", text)

    def test_launcher_rejects_untrusted_tmpdir_before_network_access(self):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d).chmod(0o777)
            env = os.environ.copy()
            env["TMPDIR"] = d
            env["HAOS_TRUENAS_INSTALLER_REF"] = "main"
            result = subprocess.run(["/bin/sh", str(ROOT / "install.sh"), "--preview"], env=env, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe temporary-directory", result.stderr)
            self.assertNotIn("Downloading HAOS TrueNAS installer archive", result.stdout)


class SafeArchiveExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        launcher = (ROOT / "install.sh").read_text()
        start = 'ROOT=$(python3 - "$TARBALL" "$EXTRACT_DIR" <<\'PY\'\n'
        end = "\nPY\n) || exit 1"
        if start not in launcher or end not in launcher:
            raise AssertionError("Could not locate the launcher's inline safe extractor")
        cls.extractor = launcher.split(start, 1)[1].split(end, 1)[0]

    def make_archive(self, path, extra_members):
        with tarfile.open(path, "w:gz") as archive:
            root = tarfile.TarInfo("repo")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            for member, data in extra_members:
                archive.addfile(member, io.BytesIO(data) if data is not None else None)

    def run_extractor(self, archive, destination):
        return subprocess.run([sys.executable, "-c", self.extractor, str(archive), str(destination)], capture_output=True, text=True)

    def test_valid_repository_archive_extracts_expected_files(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            archive = base / "source.tar.gz"
            members = []
            for name, data in (("repo/install.sh", b"#!/bin/sh\\n"), ("repo/haos_truenas_installer.py", b"print('ok')\\n")):
                info = tarfile.TarInfo(name); info.size = len(data)
                members.append((info, data))
            self.make_archive(archive, members)
            dest = base / "extracted"; dest.mkdir(mode=0o700)
            result = self.run_extractor(archive, dest)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "repo")
            self.assertEqual((dest / "repo" / "install.sh").read_bytes(), b"#!/bin/sh\\n")
            self.assertEqual((dest / "repo" / "haos_truenas_installer.py").read_bytes(), b"print('ok')\\n")

    def test_rejects_path_traversal_before_writing_outside(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d); archive = base / "source.tar.gz"; dest = base / "extracted"; dest.mkdir()
            members = []
            for name, data in (("repo/install.sh", b"x"), ("repo/haos_truenas_installer.py", b"x"), ("repo/../../outside", b"owned")):
                info = tarfile.TarInfo(name); info.size = len(data); members.append((info, data))
            self.make_archive(archive, members)
            result = self.run_extractor(archive, dest)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe path", result.stderr)
            self.assertFalse((base / "outside").exists())

    def test_rejects_symlink_hardlink_and_fifo_members(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                base = pathlib.Path(d); archive = base / "source.tar.gz"; dest = base / "extracted"; dest.mkdir()
                members = []
                for name, data in (("repo/install.sh", b"x"), ("repo/haos_truenas_installer.py", b"x")):
                    info = tarfile.TarInfo(name); info.size = len(data); members.append((info, data))
                link = tarfile.TarInfo("repo/unsafe"); link.type = kind; link.linkname = "../../outside"
                members.append((link, None))
                self.make_archive(archive, members)
                result = self.run_extractor(archive, dest)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("link or unsupported", result.stderr)
                self.assertFalse((base / "outside").exists())
    def test_rejects_duplicate_paths_and_multiple_roots(self):
        cases = (
            (("repo/install.sh", b"x"), ("repo/haos_truenas_installer.py", b"x"), ("repo/install.sh", b"again")),
            (("repo/install.sh", b"x"), ("repo/haos_truenas_installer.py", b"x"), ("other/file", b"x")),
        )
        for memberspec in cases:
            with self.subTest(members=memberspec), tempfile.TemporaryDirectory() as d:
                base = pathlib.Path(d); archive = base / "source.tar.gz"; dest = base / "extracted"; dest.mkdir()
                members = []
                for name, data in memberspec:
                    info = tarfile.TarInfo(name); info.size = len(data); members.append((info, data))
                self.make_archive(archive, members)
                result = self.run_extractor(archive, dest)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((dest / "repo" / "install.sh").exists())

    def test_rejects_malformed_archive(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d); archive = base / "source.tar.gz"; archive.write_bytes(b"not a tar archive")
            dest = base / "extracted"; dest.mkdir()
            result = self.run_extractor(archive, dest)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(list(dest.iterdir()))


class StorageSelectionTests(unittest.TestCase):
    def test_pool_choice_requires_explicit_selection_and_shows_capacity(self):
        pools=[{"name":"bulk","free":900*1024**3,"size":1000*1024**3},{"name":"apps","free":100*1024**3,"size":200*1024**3}]
        with mock.patch.object(installer,"ask",return_value="2") as ask:
            self.assertEqual(installer.select_storage_pool(pools),"apps")
        ask.assert_called_once_with("Choose destination pool number","")

    def test_disk_size_selector_offers_preset_and_custom_values(self):
        with mock.patch.object(installer,"ask",return_value="3"):
            self.assertEqual(installer.select_disk_size(),128)
        with mock.patch.object(installer,"ask",side_effect=["5","384"]):
            self.assertEqual(installer.select_disk_size(),384)

    def test_test_limits_fix_interactive_disk_size_at_32_gib(self):
        with mock.patch.object(installer,"ask") as ask:
            self.assertEqual(installer.select_disk_size(test_limits=True),32)
        ask.assert_not_called()


class StaticSafetyTests(unittest.TestCase):
    def test_version_gate(self):
        self.assertTrue(installer.version_supported("TrueNAS-25.10.0"))
        self.assertTrue(installer.version_supported("TrueNAS-26.0.0-BETA.2"))
        self.assertFalse(installer.version_supported("TrueNAS-24.10.2"))
        self.assertFalse(installer.version_supported("TrueNAS-27.0.0"))

    def test_name_validation_rejects_shell_chars(self):
        for bad in ["", "../../x", "haos;rm", "haos $(x)", "-bad"]:
            with self.assertRaises(installer.InstallerError):
                installer.validate_name(bad, "test")
        self.assertEqual(installer.validate_name("haos_test-1", "test"), "haos_test-1")

    def test_true_nas_vm_name_must_be_alphanumeric(self):
        self.assertEqual(installer.validate_vm_name("HAOSTEST20261015105852"), "HAOSTEST20261015105852")
        for bad in ["haos-test", "haos_test", "haos.test", ""]:
            with self.assertRaisesRegex(installer.InstallerError, "only 1-63 ASCII letters and numbers"):
                installer.validate_vm_name(bad)

    def test_preview_skips_host_tools(self):
        with mock.patch.object(installer.shutil, "which", return_value=None):
            installer.require_tools_for_install(preview=True)
        with mock.patch.object(installer.shutil, "which", return_value=None):
            with self.assertRaises(installer.InstallerError):
                installer.require_tools_for_install(preview=False)

    def test_random_mac_is_local_unicast(self):
        mac = installer.random_mac()
        first = int(mac.split(":")[0], 16)
        self.assertEqual(first & 1, 0)
        self.assertEqual(first & 2, 2)

class TLSVerificationTests(unittest.TestCase):
    def test_default_tls_context_verifies_certificates_and_hostnames(self):
        context = installer.create_tls_context()
        self.assertEqual(context.verify_mode, installer.ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_custom_ca_file_is_added_to_default_trust(self):
        with tempfile.TemporaryDirectory() as d:
            ca = pathlib.Path(d) / "ca.pem"; ca.write_text("test certificate")
            expected = mock.Mock()
            with mock.patch.object(installer.ssl, "create_default_context", return_value=expected) as create:
                context = installer.create_tls_context(str(ca))
            create.assert_called_once_with()
            expected.load_verify_locations.assert_called_once_with(cafile=str(ca))
            self.assertIs(context, expected)

    def test_ca_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d) / "ca.pem"; target.write_text("certificate")
            link = pathlib.Path(d) / "link.pem"; link.symlink_to(target)
            with self.assertRaisesRegex(installer.InstallerError, "not a symlink"):
                installer.create_tls_context(str(link))

    def test_insecure_tls_requires_explicit_flag_and_disables_both_checks(self):
        context = installer.create_tls_context(insecure_tls=True)
        self.assertEqual(context.verify_mode, installer.ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)

    def test_websocket_client_uses_certificate_and_hostname_validation_by_default(self):
        fake_socket = mock.Mock()
        fake_socket.recv.return_value = installer.json.dumps({"id": 1, "result": True})
        websocket_module = mock.Mock()
        websocket_module.__version__ = "1.9.0"
        websocket_module.create_connection.return_value = fake_socket
        with mock.patch.object(installer, "websocket", websocket_module):
            client = installer.TrueNASClient("https://nas.example", "user", "password", None)
        options = websocket_module.create_connection.call_args.kwargs["sslopt"]
        self.assertEqual(options["cert_reqs"], installer.ssl.CERT_REQUIRED)
        self.assertTrue(options["check_hostname"])
        client.close()

    def test_old_websocket_client_refuses_wss_without_hostname_verification(self):
        websocket_module = mock.Mock(); websocket_module.__version__ = "0.17.2"
        with mock.patch.object(installer, "websocket", websocket_module):
            with self.assertRaisesRegex(installer.InstallerError, "0.18.0 or newer"):
                installer.TrueNASClient("wss://nas.example", "user", "password", None)
        websocket_module.create_connection.assert_not_called()

    def test_old_websocket_client_falls_back_to_verified_websockets_adapter(self):
        old_websocket = mock.Mock(); old_websocket.__version__ = "0.17.2"
        fake_socket = mock.Mock(); fake_socket.recv.return_value = installer.json.dumps({"id": 1, "result": True})
        connect = mock.Mock(return_value=fake_socket)
        with mock.patch.object(installer, "websocket", old_websocket), mock.patch.object(installer, "ws_sync_connect", connect):
            client = installer.TrueNASClient("wss://nas.example", "user", "password", None)
        context = connect.call_args.kwargs["ssl"]
        self.assertEqual(context.verify_mode, installer.ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        old_websocket.create_connection.assert_not_called()
        client.close()

    def test_websockets_adapter_receives_verified_tls_context(self):
        fake_socket = mock.Mock()
        fake_socket.recv.return_value = installer.json.dumps({"id": 1, "result": True})
        connect = mock.Mock(return_value=fake_socket)
        with mock.patch.object(installer, "websocket", None), mock.patch.object(installer, "ws_sync_connect", connect):
            client = installer.TrueNASClient("wss://nas.example", "user", "password", None)
        context = connect.call_args.kwargs["ssl"]
        self.assertEqual(context.verify_mode, installer.ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        client.close()

    def test_websocket_client_custom_ca_and_insecure_override_are_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            ca = pathlib.Path(d) / "ca.pem"; ca.write_text("certificate")
            fake_socket = mock.Mock()
            fake_socket.recv.return_value = installer.json.dumps({"id": 1, "result": True})
            websocket_module = mock.Mock(); websocket_module.__version__ = "1.9.0"; websocket_module.create_connection.return_value = fake_socket
            with mock.patch.object(installer, "create_tls_context", return_value=installer.ssl.create_default_context()):
                with mock.patch.object(installer, "websocket", websocket_module):
                    client = installer.TrueNASClient("wss://nas.example", "user", "password", None, tls_ca_cert=str(ca))
            options = websocket_module.create_connection.call_args.kwargs["sslopt"]
            self.assertEqual(options["ca_certs"], str(ca))
            self.assertEqual(options["cert_reqs"], installer.ssl.CERT_REQUIRED)
            client.close()
            fake_socket = mock.Mock(); fake_socket.recv.return_value = installer.json.dumps({"id": 1, "result": True})
            websocket_module.create_connection.return_value = fake_socket
            with mock.patch.object(installer, "websocket", websocket_module):
                client = installer.TrueNASClient("wss://nas.example", "user", "password", None, insecure_tls=True)
            options = websocket_module.create_connection.call_args.kwargs["sslopt"]
            self.assertEqual(options["cert_reqs"], installer.ssl.CERT_NONE)
            self.assertFalse(options["check_hostname"])
            client.close()

    def test_https_redirect_handler_rejects_downgrade(self):
        handler = installer.HTTPSOnlyRedirectHandler()
        request = installer.urllib.request.Request("https://trusted.example/file")
        with self.assertRaisesRegex(installer.InstallerError, "leaves HTTPS"):
            handler.redirect_request(request, None, 302, "Found", {}, "http://attacker.example/file")


class MainFlowSafetyTests(unittest.TestCase):
    def test_preview_returns_before_install_path(self):
        client = mock.Mock()
        preview = installer.Plan(True, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haos", "haos", "br0", 2, 2048, 32, "https://example.test/image.xz", "a" * 64, 10, False)
        with mock.patch.object(installer, "use_local_midclt", return_value=True), \
             mock.patch.object(installer, "MidcltClient", return_value=client), \
             mock.patch.object(installer, "lockfile", return_value=mock.MagicMock()), \
             mock.patch.object(installer, "build_plan", return_value=(preview, {})), \
             mock.patch.object(installer, "print_plan"), \
             mock.patch.object(installer, "install") as install:
            self.assertEqual(installer.main(["--preview"]), 0)
        install.assert_not_called()
        client.close.assert_called_once_with()


class ReleaseMetadataTests(unittest.TestCase):
    def response_for(self, asset_url, digest="sha256:" + "a" * 64):
        metadata = [{"draft":False,"prerelease":False,"tag_name":"18.3","assets":[{"name":"haos_ova-18.3.qcow2.xz","browser_download_url":asset_url,"digest":digest,"size":1234}]}]
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = installer.json.dumps(metadata)
        response.geturl.return_value = installer.HAOS_REPO_API
        return response

    def test_accepts_expected_official_https_asset_metadata(self):
        url = "https://github.com/home-assistant/operating-system/releases/download/18.3/haos_ova-18.3.qcow2.xz"
        with mock.patch.object(installer, "open_https_url", return_value=self.response_for(url)):
            asset = installer.fetch_latest_haos_asset()
        self.assertEqual(asset["url"], url)
        self.assertEqual(asset["sha256"], "a" * 64)
        self.assertEqual(asset["size"], 1234)

    def test_rejects_unexpected_asset_host_path_and_digest(self):
        cases = (
            ("https://attacker.example/haos.qcow2.xz", "sha256:" + "a" * 64),
            ("https://github.com/attacker/repo/releases/download/18.3/haos_ova-18.3.qcow2.xz", "sha256:" + "a" * 64),
            ("https://github.com/home-assistant/operating-system/releases/download/18.3/haos_ova-18.3.qcow2.xz", "sha256:bad"),
        )
        for url, digest in cases:
            with self.subTest(url=url, digest=digest), mock.patch.object(installer, "open_https_url", return_value=self.response_for(url, digest)):
                with self.assertRaises(installer.InstallerError):
                    installer.fetch_latest_haos_asset()


class DownloadSafetyTests(unittest.TestCase):
    def test_default_cache_is_scoped_to_effective_user(self):
        args = installer.parse_args([])
        self.assertEqual(args.cache_dir, f"/tmp/haos-truenas-installer-cache-{installer.os.geteuid()}")

    def test_cache_rejects_foreign_owned_directory_with_actionable_message(self):
        actual_uid = installer.os.geteuid()
        with tempfile.TemporaryDirectory() as d:
            cache = pathlib.Path(d) / "cache"; cache.mkdir()
            with mock.patch.object(installer.os, "geteuid", return_value=actual_uid + 1):
                with self.assertRaisesRegex(installer.InstallerError, "not owned by root or the current user|choose a new --cache-dir"):
                    installer.ensure_private_cache_dir(cache)

    def test_cache_rejects_group_world_writable_parent(self):
        with tempfile.TemporaryDirectory() as d:
            shared = pathlib.Path(d) / "shared"
            shared.mkdir()
            shared.chmod(0o777)
            with self.assertRaisesRegex(installer.InstallerError, "group/world-writable directory"):
                installer.ensure_private_cache_dir(shared / "cache")

    class Response:
        def __init__(self, chunks, headers=None, url="https://example.test/image.xz"):
            self.chunks = iter(chunks)
            self.headers = headers or {}
            self.url = url
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, size=-1): return next(self.chunks, b"")
        def geturl(self): return self.url

    def test_refuses_non_https_image(self):
        with self.assertRaises(installer.InstallerError):
            installer.download_and_verify("http://example.test/image.xz", "a"*64, pathlib.Path("/unused"))
        with self.assertRaisesRegex(installer.InstallerError, "embedded credentials"):
            installer.open_https_url("https://user:secret@example.test/image.xz", timeout=1)

    def test_download_limit_removes_partial_file(self):
        response = self.Response([b"ab", b"cd", b""])
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(installer, "open_https_url", return_value=response):
                with self.assertRaisesRegex(installer.InstallerError, "download limit"):
                    installer.download_and_verify("https://example.test/image.xz", "a"*64, pathlib.Path(d), max_bytes=3)
            self.assertEqual(list(pathlib.Path(d).iterdir()), [])

    def test_download_requires_expected_size_match_and_caches_by_digest(self):
        data = b"verified image bytes"
        digest = installer.hashlib.sha256(data).hexdigest()
        response = self.Response([data, b""])
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(installer, "open_https_url", return_value=response):
                with self.assertRaisesRegex(installer.InstallerError, "size mismatch"):
                    installer.download_and_verify("https://example.test/any-name", digest, pathlib.Path(d), max_bytes=100, expected_size=len(data)+1)
            self.assertEqual(list(pathlib.Path(d).iterdir()), [])
            response = self.Response([data, b""])
            with mock.patch.object(installer, "open_https_url", return_value=response):
                saved = installer.download_and_verify("https://example.test/any-name", digest, pathlib.Path(d), max_bytes=100, expected_size=len(data))
            self.assertEqual(saved.read_bytes(), data)
            self.assertEqual(saved.name, f"haos-{digest}.qcow2.xz")

    def test_checksum_mismatch_removes_partial_download(self):
        data = b"untrusted image bytes"
        response = self.Response([data, b""])
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(installer, "open_https_url", return_value=response):
                with self.assertRaisesRegex(installer.InstallerError, "checksum mismatch"):
                    installer.download_and_verify("https://example.test/image.xz", "0"*64, pathlib.Path(d), max_bytes=100)
            self.assertEqual(list(pathlib.Path(d).iterdir()), [])

    def test_refuses_symlink_cache_entry(self):
        data = b"cache content"; digest = installer.hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            cache = pathlib.Path(d) / "cache"; cache.mkdir(mode=0o700)
            outside = pathlib.Path(d) / "outside"; outside.write_bytes(data)
            (cache / f"haos-{digest}.qcow2.xz").symlink_to(outside)
            with self.assertRaisesRegex(installer.InstallerError, "unsafe or unowned"):
                installer.download_and_verify("https://example.test/image.xz", digest, cache)
            self.assertEqual(outside.read_bytes(), data)

    def test_existing_manifest_is_not_overwritten(self):
        plan = installer.Plan(True, "version", "tank", "tank/haos-vms/haos", "haos", "br0", 2, 4096, 64, "https://example.test/i", "a"*64, 10, False)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "manifest.json"
            path.write_text("keep me")
            with self.assertRaisesRegex(installer.InstallerError, "already exists"):
                installer.create_manifest(path, plan)
            self.assertEqual(path.read_text(), "keep me")


class ImagePreparationTests(unittest.TestCase):
    def test_prepares_seekable_qcow_and_cleans_it(self):
        with tempfile.TemporaryDirectory() as d:
            source = pathlib.Path(d) / "haos.xz"
            with open(source, "wb") as f:
                installer.subprocess.run(["xz", "-c"], input=b"qcow image", stdout=f, check=True)
            real_run = installer.subprocess.run
            def run(cmd, **kwargs):
                if cmd[0] == "qemu-img":
                    return installer.subprocess.CompletedProcess(cmd, 0, stdout='{"format":"qcow2","virtual-size":1073741824}', stderr="")
                return real_run(cmd, **kwargs)
            space = mock.Mock(f_bavail=40, f_frsize=1024**3)
            with mock.patch.object(installer.subprocess, "run", side_effect=run), mock.patch.object(installer.os, "statvfs", return_value=space):
                with installer.prepared_qcow_image(source, pathlib.Path(d), 16) as qcow:
                    self.assertTrue(qcow.is_file())
                    self.assertEqual(qcow.read_bytes(), b"qcow image")
                self.assertFalse(qcow.exists())

    def test_expansion_limit_refuses_before_writing_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            source = pathlib.Path(d) / "haos.xz"
            source.write_bytes(b"compressed")
            real_run = installer.subprocess.run
            def report_oversize(cmd, **kwargs):
                if cmd[:2] == ["xz", "--robot"]:
                    size = 16*1024**3 + 256*1024**2 + 1
                    row = ["file", "1", "1", "72", str(size), "---", "CRC64", "0"]
                    return installer.subprocess.CompletedProcess(cmd, 0, stdout=chr(9).join(row) + chr(10), stderr="")
                return real_run(cmd, **kwargs)
            with mock.patch.object(installer.subprocess, "run", side_effect=report_oversize):
                with self.assertRaisesRegex(installer.InstallerError, "safe limit"):
                    with installer.prepared_qcow_image(source, pathlib.Path(d), 16):
                        self.fail("oversized image should not be decompressed")
            self.assertEqual([p.name for p in pathlib.Path(d).iterdir()], ["haos.xz"])

    def test_rejects_backing_file_before_yield_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as d:
            source = pathlib.Path(d) / "haos.xz"
            with open(source, "wb") as f:
                installer.subprocess.run(["xz", "-c"], input=b"qcow image", stdout=f, check=True)
            real_run = installer.subprocess.run
            def run(cmd, **kwargs):
                if cmd[0] == "qemu-img":
                    return installer.subprocess.CompletedProcess(cmd, 0, stdout='{"format":"qcow2","virtual-size":1073741824,"backing-filename":"/tmp/base.qcow2"}', stderr="")
                return real_run(cmd, **kwargs)
            space = mock.Mock(f_bavail=40, f_frsize=1024**3)
            with mock.patch.object(installer.subprocess, "run", side_effect=run), mock.patch.object(installer.os, "statvfs", return_value=space):
                with self.assertRaisesRegex(installer.InstallerError, "backing file"):
                    with installer.prepared_qcow_image(source, pathlib.Path(d), 16):
                        self.fail("untrusted backing-chain image was yielded")
            self.assertEqual([p.name for p in pathlib.Path(d).iterdir()], ["haos.xz"])


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
    def call(self, method, params=None, *extra):
        self.calls.append((method, params, extra))
        val = self.responses[method]
        return val(params) if callable(val) else val

class PlanTests(unittest.TestCase):
    def base_client(self):
        methods = {m:{} for m in ["vm.create","vm.start","vm.stop","vm.query","vm.device.create","vm.device.delete","vm.device.query","vm.device.nic_attach_choices","pool.dataset.create","pool.dataset.query","pool.query","interface.query"]}
        return FakeClient({
            "system.info": {"physmem": 16*1024**3},
            "system.version": "TrueNAS-26.0.0-BETA.2",
            "core.get_methods": methods,
            "pool.query": [{"name":"tank","healthy":True,"status":"ONLINE","free":500*1024**3,"size":1000*1024**3}],
            "vm.device.nic_attach_choices": {"BRIDGE":["br0"], "MACVLAN":[]},
            "vm.query": [],
            "vm.device.query": [],
            "pool.dataset.query": 0,
        })
    def args(self, **kw):
        ns = installer.parse_args(["--preview","--pool","tank","--nic","br0","--vm-name","haos","--vcpus","2","--ram-mib","4096","--disk-gib","64","--image-url","https://example/haos.qcow2.xz","--image-sha256","a"*64])
        for k,v in kw.items(): setattr(ns,k,v)
        return ns
    def test_build_plan_preview_no_writes_and_defaults(self):
        client = self.base_client()
        plan, meta = installer.build_plan(client, self.args())
        self.assertTrue(plan.preview)
        self.assertEqual(plan.vcpus, 2)
        self.assertEqual(plan.ram_mib, 4096)
        self.assertEqual(plan.disk_gib, 64)
        write_calls = [c for c in client.calls if c[0] in {"vm.create","pool.dataset.create","vm.device.create","vm.start"}]
        self.assertEqual(write_calls, [])
    def test_invalid_pool_path_is_rejected_before_resource_checks(self):
        client = self.base_client()
        with self.assertRaisesRegex(installer.InstallerError, "Invalid pool name"):
            installer.build_plan(client, self.args(pool="tank/../other"))

    def test_duplicate_vm_refused(self):
        client = self.base_client()
        client.responses["vm.query"] = [{"name":"haos"}]
        with self.assertRaises(installer.InstallerError):
            installer.build_plan(client, self.args())
    def test_cli_nic_must_be_an_existing_attachment_choice(self):
        client = self.base_client()
        with self.assertRaisesRegex(installer.InstallerError, "not an existing TrueNAS VM attachment choice"):
            installer.build_plan(client, self.args(nic="not-a-real-bridge"))
        self.assertFalse(any(c[0] in {"pool.dataset.create", "vm.create", "vm.device.create"} for c in client.calls))

    def test_existing_zvol_refused(self):
        client = self.base_client()
        client.responses["pool.dataset.query"] = 1
        with self.assertRaises(installer.InstallerError):
            installer.build_plan(client, self.args())
    def test_unsupported_version_cannot_be_bypassed_for_live_install(self):
        client = self.base_client()
        client.responses["system.version"] = "TrueNAS-27.0.0-BETA.1"
        args = self.args(preview=False, allow_unsupported_version=True)
        with self.assertRaisesRegex(installer.InstallerError, "live installs remain blocked"):
            installer.build_plan(client, args)
        self.assertFalse(any(c[0] in {"vm.create", "pool.dataset.create", "vm.device.create", "vm.start"} for c in client.calls))
    def test_unsupported_version_preview_can_be_evaluated_read_only(self):
        client = self.base_client()
        client.responses["system.version"] = "TrueNAS-27.0.0-BETA.1"
        args = self.args(allow_unsupported_version=True)
        plan, _ = installer.build_plan(client, args)
        self.assertTrue(plan.preview)
        self.assertEqual(plan.true_version, "TrueNAS-27.0.0-BETA.1")
    def test_minimum_supported_resources_accepted(self):
        client = self.base_client()
        plan, _ = installer.build_plan(client, self.args(vcpus=2, ram_mib=2048, disk_gib=32))
        self.assertEqual((plan.vcpus, plan.ram_mib, plan.disk_gib), (2, 2048, 32))
    def test_invalid_cli_resource_values_refused(self):
        for changes in ({"vcpus": 1}, {"ram_mib": 2047}, {"disk_gib": 31}, {"max_download_gib": 0}):
            client = self.base_client()
            with self.subTest(changes=changes), self.assertRaises(installer.InstallerError):
                installer.build_plan(client, self.args(**changes))

    def test_resume_manifest_accepts_only_exact_manifest_owned_partial_resources(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haostest", "haostest", "br0", 2, 2048, 32, "https://example.test/haos.qcow2.xz", "a"*64, 1234, False)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "manifest.json"
            path.write_text(installer.json.dumps({"tool":installer.APP,"plan":installer.dataclasses.asdict(plan),"resources":[{"type":"dataset","id":"tank/haos-vms"},{"type":"zvol","id":plan.zvol,"dev":"/dev/zvol/"+plan.zvol}]}))
            args = installer.parse_args(["--resume-manifest", str(path), "--no-start"])
            client = self.base_client()
            def dataset_query(params):
                target = params[0][0][2]
                if target == plan.zvol:
                    return [{"id":plan.zvol,"type":"VOLUME","volsize":{"parsed":32*1024**3},"user_properties":{"comments":{"parsed":f"Created by {installer.APP} for {plan.vm_name}"}}}]
                if target == "tank/haos-vms":
                    return [{"id":"tank/haos-vms","type":"FILESYSTEM","user_properties":{"comments":{"parsed":f"Created by {installer.APP} for HAOS VM disks"}}}]
                return []
            client.responses["pool.dataset.query"] = dataset_query
            resumed, meta = installer.build_plan(client, args)
            self.assertEqual(resumed, plan)
            self.assertTrue(meta["resume"])
            self.assertFalse(any(c[0] in {"vm.create","pool.dataset.create","vm.device.create","vm.start"} for c in client.calls))

    def test_resume_can_override_invalid_saved_vm_name_without_changing_zvol(self):
        saved = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haos-test-20261015-105852", "haos-test-20261015-105852", "br0", 2, 2048, 32, "https://example.test/haos.qcow2.xz", "a"*64, 1234, False)
        new_name="HAOSTEST20261015105852"
        with tempfile.TemporaryDirectory() as d:
            path=pathlib.Path(d)/"manifest.json"
            resources=[{"type":"dataset","id":"tank/haos-vms"},{"type":"zvol","id":saved.zvol,"dev":"/dev/zvol/"+saved.zvol}]
            path.write_text(installer.json.dumps({"tool":installer.APP,"plan":installer.dataclasses.asdict(saved),"resources":resources}))
            args=installer.parse_args(["--resume-manifest",str(path),"--vm-name",new_name,"--no-start"])
            client=self.base_client()
            def dataset_query(params):
                target=params[0][0][2]
                if target==saved.zvol: return [{"id":saved.zvol,"type":"VOLUME","volsize":{"parsed":32*1024**3},"user_properties":{"comments":{"parsed":f"Created by {installer.APP} for {saved.vm_name}"}}}]
                return [{"id":"tank/haos-vms","type":"FILESYSTEM","user_properties":{"comments":{"parsed":f"Created by {installer.APP} for HAOS VM disks"}}}]
            client.responses["pool.dataset.query"]=dataset_query
            resumed,meta=installer.build_plan(client,args)
            self.assertEqual(resumed.vm_name,new_name)
            self.assertEqual(resumed.zvol,saved.zvol)
            self.assertTrue(meta["resume"])

    def test_resume_refuses_unmarked_or_referenced_zvols_and_saved_vm_name(self):
        saved = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/partial", "OldName", "br0", 2, 2048, 32, "https://example.test/haos.qcow2.xz", "a"*64, 1234, False)
        new_name = "NewName"
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "manifest.json"
            resources = [{"type":"dataset","id":"tank/haos-vms"},{"type":"zvol","id":saved.zvol,"dev":"/dev/zvol/"+saved.zvol}]
            path.write_text(installer.json.dumps({"tool":installer.APP,"plan":installer.dataclasses.asdict(saved),"resources":resources}))
            args = installer.parse_args(["--resume-manifest",str(path),"--vm-name",new_name,"--no-start"])
            for case in ("unmarked", "vm", "device"):
                with self.subTest(case=case):
                    client = self.base_client()
                    def dataset_query(params):
                        target = params[0][0][2]
                        comment = "someone else" if case == "unmarked" and target == saved.zvol else f"Created by {installer.APP} for {saved.vm_name}"
                        if target == saved.zvol:
                            return [{"id":saved.zvol,"type":"VOLUME","volsize":{"parsed":32*1024**3},"user_properties":{"comments":{"parsed":comment}}}]
                        return [{"id":"tank/haos-vms","type":"FILESYSTEM","user_properties":{"comments":{"parsed":f"Created by {installer.APP} for HAOS VM disks"}}}]
                    client.responses["pool.dataset.query"] = dataset_query
                    if case == "vm": client.responses["vm.query"] = [{"name":saved.vm_name}]
                    if case == "device": client.responses["vm.device.query"] = [{"attributes":{"path":"/dev/zvol/"+saved.zvol}}]
                    with self.assertRaises(installer.InstallerError):
                        installer.build_plan(client,args)

    def test_resume_install_reuses_owned_zvol_and_creates_only_new_vm_devices(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haos-test-20261015-105852", "HAOSTEST20261015105852", "br0", 2, 2048, 32, "https://example.test/haos.qcow2.xz", "a"*64, 1234, False)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "manifest.json"
            resources = [{"type":"dataset","id":"tank/haos-vms"},{"type":"zvol","id":plan.zvol,"dev":"/dev/zvol/"+plan.zvol}]
            saved_plan = installer.dataclasses.replace(plan,vm_name="haos-test-20261015-105852")
            path.write_text(installer.json.dumps({"tool":installer.APP,"plan":installer.dataclasses.asdict(saved_plan),"resources":resources}))
            args = installer.parse_args(["--resume-manifest", str(path), "--vm-name", plan.vm_name, "--no-start"])
            class ResumeClient:
                def __init__(self): self.calls=[]
                def call(self, method, params=None):
                    self.calls.append((method,params))
                    if method == "pool.dataset.query":
                        target=params[0][0][2]
                        if target == plan.zvol: return [{"id":plan.zvol,"type":"VOLUME","volsize":{"parsed":32*1024**3},"user_properties":{"comments":{"parsed":f"Created by {installer.APP} for {saved_plan.vm_name}"}}}]
                        return [{"id":"tank/haos-vms","type":"FILESYSTEM","user_properties":{"comments":{"parsed":f"Created by {installer.APP} for HAOS VM disks"}}}]
                    if method == "vm.query": return []
                    if method == "vm.device.query": return []
                    if method == "vm.create": return {"id":7}
                    if method == "vm.device.create": return {"id":len([c for c in self.calls if c[0] == method])}
                    raise AssertionError(f"Unexpected middleware call: {method}")
            client=ResumeClient()
            @installer.contextlib.contextmanager
            def prepared(*unused):
                yield pathlib.Path(d) / "prepared.qcow2"
            actual_uid = installer.os.geteuid()
            uid_calls = 0
            def effective_uid():
                nonlocal uid_calls
                uid_calls += 1
                return 0 if uid_calls == 1 else actual_uid
            with installer.contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(installer.os,"geteuid",side_effect=effective_uid))
                stack.enter_context(mock.patch.object(installer,"require_tools_for_install"))
                stack.enter_context(mock.patch.object(installer,"download_and_verify",return_value=pathlib.Path(d)/"verified.xz"))
                stack.enter_context(mock.patch.object(installer,"prepared_qcow_image",prepared))
                stack.enter_context(mock.patch.object(installer.Path,"exists",return_value=True))
                stack.enter_context(mock.patch.object(installer.os,"access",return_value=True))
                convert=stack.enter_context(mock.patch.object(installer,"convert_qcow_to_zvol"))
                installer.install(client,plan,args)
            self.assertEqual([c[0] for c in client.calls].count("pool.dataset.create"),0)
            self.assertEqual([c[0] for c in client.calls].count("vm.create"),1)
            self.assertEqual([c[0] for c in client.calls].count("vm.device.create"),2)
            self.assertNotIn("vm.start",[c[0] for c in client.calls])
            convert.assert_called_once()
            final_manifest=installer.json.loads(path.read_text())
            self.assertEqual([r["type"] for r in final_manifest["resources"]], ["dataset","zvol","vm","device","device"])
            self.assertEqual(final_manifest["plan"]["vm_name"], plan.vm_name)

    def test_resume_preview_stays_read_only(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haostest", "haostest", "br0", 2, 2048, 32, "https://example.test/haos.qcow2.xz", "a"*64, 1234, False)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "manifest.json"
            path.write_text(installer.json.dumps({"tool":installer.APP,"plan":installer.dataclasses.asdict(plan),"resources":[{"type":"zvol","id":plan.zvol,"dev":"/dev/zvol/"+plan.zvol}]}))
            args = installer.parse_args(["--preview", "--resume-manifest", str(path)])
            client = self.base_client()
            client.responses["pool.dataset.query"] = lambda params: ([{"id":plan.zvol,"type":"VOLUME","volsize":{"parsed":32*1024**3},"user_properties":{"comments":{"parsed":f"Created by {installer.APP} for {plan.vm_name}"}}}] if params[0][0][2] == plan.zvol else [{"id":"tank/haos-vms","type":"FILESYSTEM"}])
            preview, _ = installer.build_plan(client, args)
            self.assertTrue(preview.preview)

    def test_resume_rejects_untracked_vm_or_device_resources(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haostest", "haostest", "br0", 2, 2048, 32, "https://example.test/haos.qcow2.xz", "a"*64, 1234, False)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "manifest.json"
            path.write_text(installer.json.dumps({"tool":installer.APP,"plan":installer.dataclasses.asdict(plan),"resources":[{"type":"zvol","id":plan.zvol,"dev":"/dev/zvol/"+plan.zvol},{"type":"vm","name":"other"}]}))
            args = installer.parse_args(["--resume-manifest", str(path)])
            with self.assertRaisesRegex(installer.InstallerError, "no VM or devices"):
                installer.build_plan(self.base_client(), args)

    def test_fresh_install_records_disk_before_nic_creation_can_fail(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haostest", "haostest", "br0", 2, 2048, 32, "https://example.test/haos.qcow2.xz", "a"*64, 1234, False)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "manifest.json"
            args = installer.parse_args(["--vm-name", plan.vm_name, "--manifest", str(path), "--no-start"])
            args.cache_dir = str(pathlib.Path(d) / "cache")
            class FailingNicClient:
                def __init__(self): self.calls=[]
                def call(self, method, params=None):
                    self.calls.append((method, params))
                    if method == "pool.dataset.query": return []
                    if method == "pool.dataset.create":
                        if not params:
                            raise AssertionError("pool.dataset.create is missing parameters")
                        return {"id":params[0]["name"]}
                    if method == "vm.create": return {"id":17}
                    if method == "vm.device.create":
                        if len([call for call in self.calls if call[0] == method]) == 2:
                            raise installer.InstallerError("simulated NIC creation failure")
                        return {"id":31}
                    raise AssertionError(f"Unexpected middleware call: {method}")
            client = FailingNicClient()
            @installer.contextlib.contextmanager
            def prepared(*unused):
                yield pathlib.Path(d) / "prepared.qcow2"
            actual_uid = installer.os.geteuid()
            calls = 0
            def effective_uid():
                nonlocal calls
                calls += 1
                return 0 if calls == 1 else actual_uid
            with installer.contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(installer.os, "geteuid", side_effect=effective_uid))
                stack.enter_context(mock.patch.object(installer, "require_tools_for_install"))
                stack.enter_context(mock.patch.object(installer, "download_and_verify", return_value=pathlib.Path(d) / "image.xz"))
                stack.enter_context(mock.patch.object(installer, "prepared_qcow_image", prepared))
                stack.enter_context(mock.patch.object(installer.Path, "exists", return_value=True))
                stack.enter_context(mock.patch.object(installer.os, "access", return_value=True))
                stack.enter_context(mock.patch.object(installer, "convert_qcow_to_zvol"))
                with self.assertRaisesRegex(installer.InstallerError, "simulated NIC creation failure"):
                    installer.install(client, plan, args)
            saved = installer.json.loads(path.read_text())
            self.assertEqual([resource["type"] for resource in saved["resources"]], ["dataset", "zvol", "vm", "device"])
            self.assertEqual(saved["resources"][-1], {"type":"device", "id":31, "kind":"DISK"})

if __name__ == "__main__":
    unittest.main()
