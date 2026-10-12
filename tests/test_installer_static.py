import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("installer", ROOT / "haos_truenas_installer.py")
installer = importlib.util.module_from_spec(SPEC)
sys.modules["installer"] = installer
SPEC.loader.exec_module(installer)

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

class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
    def call(self, method, params=None, *extra):
        self.calls.append((method, params, extra))
        val = self.responses[method]
        return val() if callable(val) else val

class PlanTests(unittest.TestCase):
    def base_client(self):
        methods = {m:{} for m in ["vm.create","vm.start","vm.query","vm.device.create","vm.device.nic_attach_choices","pool.dataset.create","pool.dataset.query","pool.query"]}
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

if __name__ == "__main__":
    unittest.main()
