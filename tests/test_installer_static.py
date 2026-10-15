import importlib.util
import pathlib
import sys
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

    def test_manifest_permission_error_is_actionable_and_aborts_before_download(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haos-test", "haos-test", "br0", 2, 2048, 32, "https://example.test/i", "a"*64, 10, False)
        args = installer.parse_args(["--vm-name", "haos-test"])
        with mock.patch.object(installer.os, "geteuid", return_value=0):
            with mock.patch.object(installer, "require_tools_for_install"):
                with mock.patch.object(installer.os, "open", side_effect=PermissionError):
                    with mock.patch.object(installer, "download_and_verify") as download:
                        with self.assertRaisesRegex(installer.InstallerError, "writable location"):
                            installer.install(object(), plan, args)
                        download.assert_not_called()

    def test_non_root_live_install_aborts_before_manifest_or_download(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haos-test", "haos-test", "br0", 2, 2048, 32, "https://example.test/i", "a"*64, 10, False)
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
        self.assertIn("if [ -r /dev/tty ]; then", text)
        self.assertIn('python3 "$SRC/haos_truenas_installer.py" "$@" </dev/tty', text)
        self.assertIn('else\n    python3 "$SRC/haos_truenas_installer.py" "$@"', text)


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

class DownloadSafetyTests(unittest.TestCase):
    class Response:
        def __init__(self, chunks, headers=None):
            self.chunks = iter(chunks)
            self.headers = headers or {}
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, size=-1): return next(self.chunks, b"")

    def test_refuses_non_https_image(self):
        with self.assertRaises(installer.InstallerError):
            installer.download_and_verify("http://example.test/image.xz", "a"*64, pathlib.Path("/unused"))

    def test_download_limit_removes_partial_file(self):
        response = self.Response([b"ab", b"cd", b""])
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(installer.urllib.request, "urlopen", return_value=response):
                with self.assertRaisesRegex(installer.InstallerError, "download limit"):
                    installer.download_and_verify("https://example.test/image.xz", "a"*64, pathlib.Path(d), max_bytes=3)
            self.assertEqual(list(pathlib.Path(d).iterdir()), [])

    def test_download_requires_expected_size_match_and_caches_by_digest(self):
        data = b"verified image bytes"
        digest = installer.hashlib.sha256(data).hexdigest()
        response = self.Response([data, b""])
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(installer.urllib.request, "urlopen", return_value=response):
                with self.assertRaisesRegex(installer.InstallerError, "size mismatch"):
                    installer.download_and_verify("https://example.test/any-name", digest, pathlib.Path(d), max_bytes=100, expected_size=len(data)+1)
            self.assertEqual(list(pathlib.Path(d).iterdir()), [])
            response = self.Response([data, b""])
            with mock.patch.object(installer.urllib.request, "urlopen", return_value=response):
                saved = installer.download_and_verify("https://example.test/any-name", digest, pathlib.Path(d), max_bytes=100, expected_size=len(data))
            self.assertEqual(saved.read_bytes(), data)
            self.assertEqual(saved.name, f"haos-{digest}.qcow2.xz")

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
    def test_duplicate_vm_refused(self):
        client = self.base_client()
        client.responses["vm.query"] = [{"name":"haos"}]
        with self.assertRaises(installer.InstallerError):
            installer.build_plan(client, self.args())
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
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haos-test", "haos-test", "br0", 2, 2048, 32, "https://example.test/haos.qcow2.xz", "a"*64, 1234, False)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "manifest.json"
            path.write_text(installer.json.dumps({"tool":installer.APP,"plan":installer.dataclasses.asdict(plan),"resources":[{"type":"dataset","id":"tank/haos-vms"},{"type":"zvol","id":plan.zvol,"dev":"/dev/zvol/"+plan.zvol}]}))
            args = installer.parse_args(["--resume-manifest", str(path), "--no-start"])
            client = self.base_client()
            def dataset_query(params):
                target = params[0][0][2]
                if target == plan.zvol:
                    return [{"id":plan.zvol,"type":"VOLUME","volsize":{"parsed":32*1024**3}}]
                if target == "tank/haos-vms":
                    return [{"id":"tank/haos-vms","type":"FILESYSTEM"}]
                return []
            client.responses["pool.dataset.query"] = dataset_query
            resumed, meta = installer.build_plan(client, args)
            self.assertEqual(resumed, plan)
            self.assertTrue(meta["resume"])
            self.assertFalse(any(c[0] in {"vm.create","pool.dataset.create","vm.device.create","vm.start"} for c in client.calls))

    def test_resume_install_reuses_owned_zvol_and_creates_only_new_vm_devices(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haos-test", "haos-test", "br0", 2, 2048, 32, "https://example.test/haos.qcow2.xz", "a"*64, 1234, False)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "manifest.json"
            resources = [{"type":"dataset","id":"tank/haos-vms"},{"type":"zvol","id":plan.zvol,"dev":"/dev/zvol/"+plan.zvol}]
            path.write_text(installer.json.dumps({"tool":installer.APP,"plan":installer.dataclasses.asdict(plan),"resources":resources}))
            args = installer.parse_args(["--resume-manifest", str(path), "--no-start"])
            class ResumeClient:
                def __init__(self): self.calls=[]
                def call(self, method, params=None):
                    self.calls.append((method,params))
                    if method == "pool.dataset.query":
                        target=params[0][0][2]
                        if target == plan.zvol: return [{"id":plan.zvol,"type":"VOLUME","volsize":{"parsed":32*1024**3}}]
                        return [{"id":"tank/haos-vms","type":"FILESYSTEM"}]
                    if method == "vm.query": return []
                    if method == "vm.create": return {"id":7}
                    if method == "vm.device.create": return {"id":len([c for c in self.calls if c[0] == method])}
                    raise AssertionError(f"Unexpected middleware call: {method}")
            client=ResumeClient()
            @installer.contextlib.contextmanager
            def prepared(*unused):
                yield pathlib.Path(d) / "prepared.qcow2"
            with installer.contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(installer.os,"geteuid",return_value=0))
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
            self.assertEqual([r["type"] for r in installer.json.loads(path.read_text())["resources"]], ["dataset","zvol","vm","device","device"])

    def test_resume_preview_stays_read_only(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haos-test", "haos-test", "br0", 2, 2048, 32, "https://example.test/haos.qcow2.xz", "a"*64, 1234, False)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "manifest.json"
            path.write_text(installer.json.dumps({"tool":installer.APP,"plan":installer.dataclasses.asdict(plan),"resources":[{"type":"zvol","id":plan.zvol,"dev":"/dev/zvol/"+plan.zvol}]}))
            args = installer.parse_args(["--preview", "--resume-manifest", str(path)])
            client = self.base_client()
            client.responses["pool.dataset.query"] = lambda params: ([{"id":plan.zvol,"type":"VOLUME","volsize":{"parsed":32*1024**3}}] if params[0][0][2] == plan.zvol else [{"id":"tank/haos-vms","type":"FILESYSTEM"}])
            preview, _ = installer.build_plan(client, args)
            self.assertTrue(preview.preview)

    def test_resume_rejects_untracked_vm_or_device_resources(self):
        plan = installer.Plan(False, "TrueNAS-26.0.0-BETA.2", "tank", "tank/haos-vms/haos-test", "haos-test", "br0", 2, 2048, 32, "https://example.test/haos.qcow2.xz", "a"*64, 1234, False)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "manifest.json"
            path.write_text(installer.json.dumps({"tool":installer.APP,"plan":installer.dataclasses.asdict(plan),"resources":[{"type":"zvol","id":plan.zvol,"dev":"/dev/zvol/"+plan.zvol},{"type":"vm","name":"other"}]}))
            args = installer.parse_args(["--resume-manifest", str(path)])
            with self.assertRaisesRegex(installer.InstallerError, "no VM or devices"):
                installer.build_plan(self.base_client(), args)

if __name__ == "__main__":
    unittest.main()
