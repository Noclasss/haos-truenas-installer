import base64
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ubuntu-server" / "ubuntu_server_truenas_installer.py"
SPEC = importlib.util.spec_from_file_location("ubuntu_server_installer", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load Ubuntu Server installer module")
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


def ssh_key() -> str:
    kind = b"ssh-ed25519"
    blob = len(kind).to_bytes(4, "big") + kind + b"x" * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " test@host"


class ValidationTests(unittest.TestCase):
    def test_vm_username_hostname_and_pool_validators(self):
        self.assertEqual(installer.validate_vm_name("UBUNTUSERVER01"), "UBUNTUSERVER01")
        self.assertEqual(installer.validate_username("ubuntu_admin-1"), "ubuntu_admin-1")
        self.assertEqual(installer.validate_hostname("ubuntu-server-1"), "ubuntu-server-1")
        self.assertEqual(installer.validate_pool_name("NVME2"), "NVME2")
        for value in ("bad-name", "", "../../x", "$(touch)"):
            with self.subTest(value=value), self.assertRaises(installer.InstallerError):
                installer.validate_vm_name(value)
        for value in ("Upper", "-bad", "bad user", "root;rm"):
            with self.subTest(value=value), self.assertRaises(installer.InstallerError):
                installer.validate_username(value)
        for value in ("-bad", "bad_name", "bad..host", "a" * 64):
            with self.subTest(value=value), self.assertRaises(installer.InstallerError):
                installer.validate_hostname(value)

    def test_ssh_public_key_validator_checks_type_and_blob(self):
        key = ssh_key()
        self.assertEqual(installer.validate_ssh_public_key(key), key)
        for bad in ("", "ssh-ed25519 not-base64!", "ssh-rsa " + key.split()[1], key + "\n" + key):
            with self.subTest(value=bad), self.assertRaises(installer.InstallerError):
                installer.validate_ssh_public_key(bad)

    def test_cloud_config_disables_password_ssh_and_quotes_user_values(self):
        user, meta = installer.cloud_config("ubuntu-host", "ubuntuadmin", ssh_key())
        self.assertTrue(user.startswith("#cloud-config\n"))
        self.assertIn("ssh_pwauth: false", user)
        self.assertIn("disable_root: true", user)
        self.assertIn('name: "ubuntuadmin"', user)
        self.assertIn(ssh_key(), user)
        self.assertIn('local-hostname: "ubuntu-host"', meta)
        self.assertRegex(meta, r"instance-id: \"truenas-[0-9a-f]{32}\"")


class FakeClient:
    def __init__(self):
        methods = {
            "system.info": {}, "system.version": {}, "core.get_methods": {},
            "pool.query": {}, "pool.dataset.query": {}, "pool.dataset.create": {},
            "vm.query": {}, "vm.create": {}, "vm.start": {},
            "vm.device.query": {}, "vm.device.create": {},
            "vm.device.nic_attach_choices": {},
        }
        methods["vm.device.create"] = {key: key for key in ("CDROM", "DISK", "NIC", "path", "dtype", "order")}
        methods["vm.create"] = {key: key for key in ("bootloader", "UEFI", "memory", "vcpus", "autostart", "ensure_display_device")}
        self.responses = {
            "system.version": installer.SUPPORTED_VERSION,
            "system.info": {"physmem": 64 * 1024**3},
            "core.get_methods": methods,
            "pool.query": [{"name": "NVME2", "healthy": True, "status": "ONLINE", "free": 900 * 1024**3, "size": 1000 * 1024**3}],
            "vm.device.nic_attach_choices": {"BRIDGE": ["br01"], "MACVLAN": []},
            "vm.query": [],
            "vm.device.query": [],
            "pool.dataset.query": lambda params: [],
        }
        self.calls = []

    def close(self):
        return None

    def call(self, method, params=None):
        self.calls.append((method, params))
        value = self.responses[method]
        return value(params) if callable(value) else value


def args(**overrides):
    values = ["--preview", "--pool", "NVME2", "--nic", "br01", "--vm-name", "UBUNTUTEST", "--hostname", "ubuntu-test", "--username", "ubuntuadmin", "--vcpus", "2", "--ram-mib", "4096", "--disk-gib", "64"]
    parsed = installer.parse_args(values)
    for key, value in overrides.items():
        setattr(parsed, key, value)
    return parsed


class PlanTests(unittest.TestCase):
    def test_plan_is_read_only_and_uses_pinned_image(self):
        client = FakeClient()
        plan = installer.build_plan(client, args(), ssh_key())
        self.assertEqual(plan.truenas_version, installer.SUPPORTED_VERSION)
        self.assertEqual(plan.pool, "NVME2")
        self.assertEqual(plan.zvol, "NVME2/ubuntu-server-vms/ubuntutest")
        self.assertFalse(plan.start_vm)
        self.assertEqual(plan.ssh_key_sha256, hashlib.sha256(ssh_key().encode()).hexdigest())
        write_methods = {"pool.dataset.create", "vm.create", "vm.device.create", "vm.start"}
        self.assertFalse(any(method in write_methods for method, _ in client.calls))
        self.assertTrue(installer.IMAGE_URL.endswith(installer.IMAGE_NAME))
        self.assertEqual(len(installer.IMAGE_SHA256), 64)

    def test_duplicate_vm_refused_before_writes(self):
        client = FakeClient()
        client.responses["vm.query"] = [{"name": "UBUNTUTEST"}]
        with self.assertRaisesRegex(installer.InstallerError, "already exists"):
            installer.build_plan(client, args(), ssh_key())
        self.assertFalse(any(m in {"pool.dataset.create", "vm.create", "vm.device.create"} for m, _ in client.calls))

    def test_existing_zvol_refused(self):
        client = FakeClient()
        client.responses["pool.dataset.query"] = lambda params: [{"id": "NVME2/ubuntu-server-vms/ubuntutest", "type": "VOLUME"}] if params[0][0][2].endswith("ubuntutest") else []
        with self.assertRaisesRegex(installer.InstallerError, "already exists"):
            installer.build_plan(client, args(), ssh_key())

    def test_unowned_parent_dataset_refused(self):
        client = FakeClient()
        client.responses["pool.dataset.query"] = lambda params: [{"id": "NVME2/ubuntu-server-vms", "type": "FILESYSTEM", "comments": "user data"}] if params[0][0][2] == "NVME2/ubuntu-server-vms" else []
        with self.assertRaisesRegex(installer.InstallerError, "ownership marker"):
            installer.build_plan(client, args(), ssh_key())

    def test_invalid_nic_and_unhealthy_pool_refused(self):
        client = FakeClient()
        with self.assertRaisesRegex(installer.InstallerError, "not a current TrueNAS choice"):
            installer.build_plan(client, args(nic="br99"), ssh_key())
        client = FakeClient()
        client.responses["pool.query"] = [{"name": "NVME2", "healthy": False, "status": "DEGRADED", "free": 900 * 1024**3, "size": 1000 * 1024**3}]
        with self.assertRaisesRegex(installer.InstallerError, "No healthy ONLINE"):
            installer.build_plan(client, args(), ssh_key())

    def test_pool_headroom_and_ram_reserve_are_enforced(self):
        client = FakeClient()
        client.responses["pool.query"][0]["free"] = 300 * 1024**3
        with self.assertRaisesRegex(installer.InstallerError, "20%"):
            installer.build_plan(client, args(disk_gib=250), ssh_key())
        client = FakeClient()
        with self.assertRaisesRegex(installer.InstallerError, "70%"):
            installer.build_plan(client, args(ram_mib=50000), ssh_key())

    def test_test_limits_clamp_resources_even_when_cli_requests_more(self):
        client = FakeClient()
        plan = installer.build_plan(client, args(test_limits=True, vcpus=8, ram_mib=8192, disk_gib=128), ssh_key())
        self.assertEqual((plan.vcpus, plan.ram_mib, plan.disk_gib), (2, 2048, 32))

    def test_unsupported_version_only_allowed_in_explicit_preview(self):
        client = FakeClient()
        client.responses["system.version"] = "TrueNAS-26.0.0-BETA.3"
        with self.assertRaisesRegex(installer.InstallerError, "gated"):
            installer.build_plan(client, args(preview=False), ssh_key())
        plan = installer.build_plan(client, args(preview=True, allow_unsupported_version=True), ssh_key())
        self.assertEqual(plan.truenas_version, "TrueNAS-26.0.0-BETA.3")

    def test_schema_missing_cdrom_or_boot_order_fails_closed(self):
        client = FakeClient()
        client.responses["core.get_methods"]["vm.device.create"].pop("CDROM")
        with self.assertRaisesRegex(installer.InstallerError, "CDROM"):
            installer.build_plan(client, args(), ssh_key())


class ImageTests(unittest.TestCase):
    class Response:
        def __init__(self, data):
            self.data = data
            self.headers = {"Content-Length": str(len(data))}
        def __enter__(self): return self
        def __exit__(self, *unused): return None
        def read(self, size=-1):
            data, self.data = self.data, b""
            return data
        def geturl(self): return installer.IMAGE_URL

    def test_download_checks_digest_and_exact_length_before_cache(self):
        data = b"signed image test"
        expected = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory(dir="/opt/data/cache/scratch") as directory:
            with mock.patch.object(installer, "IMAGE_SIZE", len(data)), mock.patch.object(installer, "IMAGE_SHA256", expected), mock.patch.object(installer.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = self.Response(data)
                image = installer.download_image(pathlib.Path(directory))
            self.assertEqual(image.read_bytes(), data)
            self.assertEqual(image.stat().st_mode & 0o777, 0o600)

    def test_checksum_failure_removes_partial_download(self):
        data = b"tampered"
        with tempfile.TemporaryDirectory(dir="/opt/data/cache/scratch") as directory:
            with mock.patch.object(installer, "IMAGE_SIZE", len(data)), mock.patch.object(installer, "IMAGE_SHA256", "0" * 64), mock.patch.object(installer.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = self.Response(data)
                with self.assertRaisesRegex(installer.InstallerError, "SHA-256 mismatch"):
                    installer.download_image(pathlib.Path(directory))
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])

    def test_qemu_image_must_be_standalone_qcow2_and_fit_disk(self):
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps({"format": "qcow2", "virtual-size": 8 * 1024**3}), stderr="")
        with mock.patch.object(installer.subprocess, "run", return_value=result):
            self.assertEqual(installer.inspect_image(pathlib.Path("image"), 16)["format"], "qcow2")
        backed = subprocess.CompletedProcess([], 0, stdout=json.dumps({"format": "qcow2", "virtual-size": 8 * 1024**3, "backing-filename": "base.qcow2"}), stderr="")
        with mock.patch.object(installer.subprocess, "run", return_value=backed):
            with self.assertRaisesRegex(installer.InstallerError, "backing file"):
                installer.inspect_image(pathlib.Path("image"), 16)

    def test_seed_iso_invokes_mkisofs_with_cidata_volume_label(self):
        with tempfile.TemporaryDirectory(dir="/opt/data/cache/scratch") as directory:
            base = pathlib.Path(directory)
            seed = base / "seed"
            seed.mkdir()
            (seed / "user-data").write_text("#cloud-config\n")
            (seed / "meta-data").write_text("instance-id: test\n")
            output = base / "cidata.iso"
            def fake_run(command, **kwargs):
                output.write_bytes(b"X" * 4096)
                return subprocess.CompletedProcess(command, 0, "", "")
            with mock.patch.object(installer.shutil, "which", side_effect=lambda name: "/usr/bin/xorriso" if name == "xorriso" else None), mock.patch.object(installer.subprocess, "run", side_effect=fake_run) as run:
                installer.create_seed_iso(seed, output)
            command = run.call_args.args[0]
            self.assertIn("CIDATA", command)
            self.assertIn("-as", command)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


class LauncherTests(unittest.TestCase):
    def test_launcher_refuses_unpinned_live_main_before_network_access(self):
        result = subprocess.run(["/bin/sh", str(ROOT / "ubuntu-server" / "install.sh")], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unpinned live install", result.stderr)
        self.assertNotIn("Downloading Ubuntu Server", result.stdout)

    def test_launcher_rejects_malformed_ref_before_network_access(self):
        env = os.environ.copy()
        env["UBUNTU_TRUENAS_INSTALLER_REF"] = "feature/branch"
        result = subprocess.run(["/bin/sh", str(ROOT / "ubuntu-server" / "install.sh"), "--preview"], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("lowercase 40-character commit SHA", result.stderr)
        self.assertNotIn("Downloading Ubuntu Server", result.stdout)


class ManifestAndFlowTests(unittest.TestCase):
    def make_plan(self):
        return installer.Plan(installer.SUPPORTED_VERSION, "NVME2", "NVME2/ubuntu-server-vms", "NVME2/ubuntu-server-vms/ubuntutest", "UBUNTUTEST", "ubuntu-test", "ubuntuadmin", "a" * 64, "br01", 2, 4096, 64, False)

    def test_manifest_is_private_and_excludes_the_public_key(self):
        with tempfile.TemporaryDirectory(dir="/opt/data/cache/scratch") as directory:
            path = pathlib.Path(directory) / "manifest.json"
            manifest = installer.create_manifest(path, self.make_plan())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("ssh-ed25519", path.read_text())
            self.assertEqual(manifest["image"]["sha256"], installer.IMAGE_SHA256)

    def test_existing_manifest_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(dir="/opt/data/cache/scratch") as directory:
            path = pathlib.Path(directory) / "manifest.json"
            path.write_text("preserve")
            with self.assertRaisesRegex(installer.InstallerError, "already exists"):
                installer.create_manifest(path, self.make_plan())
            self.assertEqual(path.read_text(), "preserve")

    def test_non_root_live_install_aborts_before_manifest_or_download(self):
        with tempfile.TemporaryDirectory(dir="/opt/data/cache/scratch") as directory:
            args_obj = installer.parse_args(["--manifest", str(pathlib.Path(directory) / "manifest.json"), "--cache-dir", str(pathlib.Path(directory) / "cache")])
            with mock.patch.object(installer.os, "geteuid", return_value=1000), mock.patch.object(installer, "download_image") as download:
                with self.assertRaisesRegex(installer.InstallerError, "root TrueNAS shell"):
                    installer.install(FakeClient(), self.make_plan(), args_obj, ssh_key())
            download.assert_not_called()
            self.assertFalse((pathlib.Path(directory) / "manifest.json").exists())

    def test_preview_flow_never_calls_install(self):
        client = FakeClient()
        with mock.patch.object(installer, "MidcltClient", return_value=client), mock.patch.object(installer, "install") as install:
            result = installer.main(["--preview", "--pool", "NVME2", "--nic", "br01", "--vm-name", "UBUNTUTEST", "--hostname", "ubuntu-test", "--username", "ubuntuadmin", "--vcpus", "2", "--ram-mib", "4096", "--disk-gib", "64"])
        self.assertEqual(result, 0)
        install.assert_not_called()
        self.assertFalse(any(m in {"pool.dataset.create", "vm.create", "vm.device.create", "vm.start"} for m, _ in client.calls))


if __name__ == "__main__":
    unittest.main()
