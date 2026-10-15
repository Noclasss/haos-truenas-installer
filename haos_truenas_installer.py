#!/usr/bin/env python3
"""Guided Home Assistant OS installer for TrueNAS SCALE.

BETA: this tool is intentionally conservative. Preview mode performs no writes.
Live installation creates only new resources that pass duplicate-name checks.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import getpass
import hashlib
import json
import os
import random
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import websocket  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    websocket = None
try:
    from websockets.sync.client import connect as ws_sync_connect  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    ws_sync_connect = None

APP = "haos-truenas-installer"
SUPPORTED_VERSION_PATTERNS = [
    re.compile(r"^TrueNAS-25\.10\."),
    re.compile(r"^TrueNAS-26\.0\.0-BETA\."),
]
DEFAULTS = {"vm_name": "HAOS", "vcpus": 2, "ram_mib": 4096, "disk_gib": 64}
TEST_LIMITS = {"vcpus": 2, "ram_mib": 2048, "disk_gib": 32}
MIN_POOL_FREE_FRACTION = 0.20
MAX_DOWNLOAD_BYTES_DEFAULT = 10 * 1024**3
HAOS_REPO_API = "https://api.github.com/repos/home-assistant/operating-system/releases"

class InstallerError(RuntimeError):
    pass

@dataclasses.dataclass
class Plan:
    preview: bool
    true_version: str
    pool: str
    zvol: str
    vm_name: str
    nic_attach: str
    vcpus: int
    ram_mib: int
    disk_gib: int
    image_url: str
    image_sha256: str
    image_size: int
    start_vm: bool
    autostart: bool = False

class MidcltClient:
    """Local TrueNAS client using the supported midclt command.

    This avoids installing Python packages into the TrueNAS host for the common
    one-command-on-host workflow.
    """
    def __init__(self):
        if shutil.which("midclt") is None:
            raise InstallerError("midclt was not found")
    def close(self):
        return None
    def call(self, method: str, params=None):
        cmd = ["midclt", "call", method]
        for param in params or []:
            cmd.append(json.dumps(param))
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode != 0:
            raise InstallerError(f"TrueNAS midclt {method} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        out = proc.stdout.strip()
        if out == "":
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            # midclt emits the system.version scalar without JSON quotes on some
            # TrueNAS builds; accept only that recognizable plain-text value.
            if method == "system.version" and re.fullmatch(r"TrueNAS-[A-Za-z0-9._+-]+", out):
                return out
            raise InstallerError(f"TrueNAS midclt {method} returned invalid JSON output") from exc

class TrueNASClient:
    def __init__(self, url: str, username: str|None, password: str|None, api_key: str|None, timeout: int = 60):
        self.url = normalize_ws_url(url)
        self.username = username
        self.password = password
        self.api_key = api_key
        self.timeout = timeout
        self._id = 0
        if websocket is not None:
            self.ws = websocket.create_connection(self.url, timeout=timeout, sslopt={"cert_reqs": ssl.CERT_NONE})
            self._send = self.ws.send
            self._recv = self.ws.recv
        elif ws_sync_connect is not None:
            self.ws = ws_sync_connect(self.url, open_timeout=timeout, max_size=64_000_000)
            self._send = self.ws.send
            self._recv = self.ws.recv
        else:
            raise InstallerError("Remote TrueNAS WebSocket API access requires either 'websocket-client' or 'websockets'. For the one-command TrueNAS host workflow, run without TRUENAS_URL/credentials so the installer can use local midclt instead.")
        if api_key:
            ok = self.call("auth.login_with_api_key", [api_key])
        else:
            ok = self.call("auth.login", [username, password])
        if ok is not True:
            raise InstallerError("TrueNAS authentication failed")
    def close(self):
        with contextlib.suppress(Exception): self.ws.close()
    def call(self, method: str, params=None):
        self._id += 1
        payload = {"jsonrpc":"2.0", "id": self._id, "method": method, "params": params or []}
        self._send(json.dumps(payload))
        while True:
            raw = self._recv()
            msg = json.loads(raw)
            if msg.get("id") != self._id:
                continue
            if "error" in msg:
                err = msg["error"]
                raise InstallerError(f"TrueNAS API {method} failed: {err.get('message') or err}")
            return msg.get("result")

def normalize_ws_url(url: str) -> str:
    if url.startswith("ws://") or url.startswith("wss://"):
        return url.rstrip("/")
    parsed = urllib.parse.urlparse(url if "://" in url else "http://" + url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    netloc = parsed.netloc or parsed.path
    return f"{scheme}://{netloc}/api/current"

def ask(prompt: str, default: str|None=None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")

def ask_int(prompt: str, default: int, min_value: int, max_value: int|None=None) -> int:
    while True:
        raw = ask(prompt, str(default))
        try: value = int(raw)
        except ValueError:
            print("Enter a whole number."); continue
        if value < min_value or (max_value is not None and value > max_value):
            print(f"Enter a value between {min_value} and {max_value}."); continue
        return value

def validate_name(name: str, what: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
        raise InstallerError(f"Invalid {what} name. Use 1-63 letters, numbers, dots, underscores or hyphens, starting with a letter/number.")
    return name

def version_supported(version: str) -> bool:
    return any(p.match(version) for p in SUPPORTED_VERSION_PATTERNS)

def fetch_latest_haos_asset() -> dict:
    req = urllib.request.Request(HAOS_REPO_API, headers={"Accept":"application/vnd.github+json", "User-Agent":APP})
    with urllib.request.urlopen(req, timeout=60) as resp:
        releases = json.load(resp)
    stable = next((r for r in releases if not r.get("draft") and not r.get("prerelease")), None)
    if not stable:
        raise InstallerError("No stable Home Assistant OS release found from GitHub API")
    wanted = f"haos_ova-{stable['tag_name']}.qcow2.xz"
    asset = next((a for a in stable.get("assets", []) if a.get("name") == wanted), None)
    if not asset:
        asset = next((a for a in stable.get("assets", []) if re.fullmatch(r"haos_ova-.+\.qcow2\.xz", a.get("name", ""))), None)
    if not asset:
        raise InstallerError("No HAOS OVA qcow2.xz asset found in latest stable release")
    digest = asset.get("digest") or ""
    if not digest.startswith("sha256:"):
        raise InstallerError("GitHub release asset does not provide a sha256 digest; refusing unverifiable image")
    return {"version": stable["tag_name"], "url": asset["browser_download_url"], "sha256": digest.split(":",1)[1], "size": int(asset["size"]), "name": asset["name"]}

def require_tools_for_install(preview: bool):
    needed = ["xz", "qemu-img"]
    if preview:
        return
    missing = [t for t in needed if shutil.which(t) is None]
    if missing:
        raise InstallerError("Missing required host tool(s): " + ", ".join(missing) + ". Do not install system packages automatically; install/provide these safely or use preview mode.")

def summarize_pool(pool: dict) -> str:
    free = int(pool.get("free") or 0); size = int(pool.get("size") or 0)
    pct = (free/size*100) if size else 0
    return f"{pool['name']} status={pool.get('status')} healthy={pool.get('healthy')} free={free//1024**3}GiB ({pct:.1f}%)"

def select_from(label: str, items: list[str], default: str|None=None) -> str:
    if not items:
        raise InstallerError(f"No choices available for {label}")
    print(f"\n{label}:")
    for idx,item in enumerate(items,1):
        mark = "*" if item == default else " "
        print(f"  {idx}. {item} {mark}")
    while True:
        raw = ask(f"Choose {label}", str(items.index(default)+1) if default in items else "1")
        try:
            val = items[int(raw)-1]
            return val
        except Exception:
            print("Enter a listed number.")

def build_plan(client, args) -> tuple[Plan, dict]:
    info = client.call("system.info")
    version = client.call("system.version")
    methods = client.call("core.get_methods")
    required = ["vm.create", "vm.start", "vm.stop", "vm.query", "vm.device.create", "vm.device.delete", "vm.device.query", "vm.device.nic_attach_choices", "pool.dataset.create", "pool.dataset.query", "pool.query", "interface.query"]
    missing = [m for m in required if m not in methods]
    if missing:
        raise InstallerError("Required TrueNAS VM/storage API methods are missing: " + ", ".join(missing))
    if not version_supported(version):
        if not args.preview or not args.allow_unsupported_version:
            raise InstallerError(f"Unsupported TrueNAS version {version}. This BETA supports 25.10.x and 26.0.0-BETA.x. Unsupported versions may only be evaluated in read-only preview mode with --allow-unsupported-version; live installs remain blocked.")
    pools = client.call("pool.query")
    healthy = [p for p in pools if p.get("healthy") and p.get("status") == "ONLINE"]
    if not healthy:
        raise InstallerError("No healthy ONLINE pools found")
    print("\nHealthy pools:")
    for p in healthy: print(" - " + summarize_pool(p))
    pool_name = args.pool or select_from("storage pool", [p["name"] for p in healthy], healthy[0]["name"])
    pool = next(p for p in healthy if p["name"] == pool_name)
    nic_choices = client.call("vm.device.nic_attach_choices")
    nics = (nic_choices.get("BRIDGE") or []) + (nic_choices.get("MACVLAN") or [])
    if not nics: raise InstallerError("No existing VM NIC attachment choices found. Refusing to alter host networking.")
    nic = args.nic or select_from("existing network attachment", nics, "br0" if "br0" in nics else ("br01" if "br01" in nics else nics[0]))
    vm_name = validate_name(args.vm_name or ask("VM name", DEFAULTS["vm_name"]), "VM")
    vcpus = args.vcpus if args.vcpus is not None else ask_int("CPU cores", DEFAULTS["vcpus"], 1, 16)
    ram_mib = args.ram_mib if args.ram_mib is not None else ask_int("RAM MiB", DEFAULTS["ram_mib"], 1024)
    disk_gib = args.disk_gib if args.disk_gib is not None else ask_int("Disk GiB", DEFAULTS["disk_gib"], 16)
    if args.test_limits:
        vcpus, ram_mib, disk_gib = min(vcpus,2), min(ram_mib,2048), min(disk_gib,32)
    if not 1 <= vcpus <= 16:
        raise InstallerError("CPU cores must be between 1 and 16")
    if ram_mib < 1024:
        raise InstallerError("RAM must be at least 1024 MiB")
    if disk_gib < 16:
        raise InstallerError("Disk size must be at least 16 GiB")
    if not 1 <= args.max_download_gib <= 10:
        raise InstallerError("Maximum download size must be between 1 and 10 GiB")
    existing_vms = client.call("vm.query")
    if any(v.get("name") == vm_name for v in existing_vms):
        raise InstallerError(f"A VM named {vm_name!r} already exists; refusing to overwrite")
    zvol_base = validate_name(args.zvol_name or vm_name.lower().replace("_","-").replace(".","-"), "zvol")
    zvol = f"{pool_name}/haos-vms/{zvol_base}"
    exists = bool(client.call("pool.dataset.query", [[["id","=",zvol]]]))
    if exists:
        raise InstallerError(f"Dataset/zvol {zvol!r} already exists; refusing to overwrite")
    required_bytes = disk_gib * 1024**3 + min(MAX_DOWNLOAD_BYTES_DEFAULT, args.max_download_gib * 1024**3)
    free_after = int(pool.get("free") or 0) - required_bytes
    if free_after < int(pool.get("size") or 0) * MIN_POOL_FREE_FRACTION:
        raise InstallerError("Pool would fall below 20% free after planned disk and temporary download allocation")
    image = fetch_latest_haos_asset() if not args.image_url else {"url":args.image_url,"sha256":args.image_sha256,"size":0,"version":"custom","name":"custom"}
    if not re.fullmatch(r"[0-9a-fA-F]{64}", image["sha256"] or ""):
        raise InstallerError("A valid 64-character SHA-256 is required for the HAOS image")
    return Plan(args.preview, version, pool_name, zvol, vm_name, nic, vcpus, ram_mib, disk_gib, image["url"], image["sha256"].lower(), int(image.get("size") or 0), not args.no_start, False), {"info": info, "methods_checked": required, "haos_version": image.get("version"), "haos_asset": image.get("name")}

def print_plan(plan: Plan, meta: dict):
    print("\nInstallation plan" + (" (PREVIEW - no changes will be made)" if plan.preview else ""))
    print(f"  TrueNAS version: {plan.true_version}")
    print(f"  HAOS image: {meta.get('haos_asset')} from official Home Assistant OS GitHub releases")
    print(f"  Image sha256: {plan.image_sha256}")
    print(f"  Pool: {plan.pool}")
    print(f"  New zvol: {plan.zvol} ({plan.disk_gib} GiB)")
    print(f"  New VM: {plan.vm_name} ({plan.vcpus} vCPU, {plan.ram_mib} MiB RAM, autostart disabled)")
    print(f"  NIC attaches to existing: {plan.nic_attach}")
    print("  Firmware: UEFI, OVMF_CODE.fd, Secure Boot disabled")
    print(f"  Start after creation: {plan.start_vm}")

def download_and_verify(url: str, sha256: str, cache_dir: Path, max_bytes: int = MAX_DOWNLOAD_BYTES_DEFAULT, expected_size: int|None = None) -> Path:
    if not url.startswith("https://"):
        raise InstallerError("HAOS image downloads must use HTTPS")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256 or ""):
        raise InstallerError("A valid 64-character SHA-256 is required for the HAOS image")
    if max_bytes <= 0:
        raise InstallerError("Download byte limit must be positive")
    if expected_size is not None and not 0 <= expected_size <= max_bytes:
        raise InstallerError(f"HAOS image asset size is outside the configured 0–{max_bytes} byte limit")
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"haos-{sha256.lower()}.qcow2.xz"
    if dest.exists():
        cached_hash = hashlib.sha256()
        with open(dest, "rb") as cached_file:
            for chunk in iter(lambda: cached_file.read(1024*1024), b""):
                cached_hash.update(chunk)
        if cached_hash.hexdigest().lower() == sha256.lower() and (expected_size is None or dest.stat().st_size == expected_size):
            return dest
        dest.unlink()
    partial = None
    partial_fd = None
    h = hashlib.sha256()
    total = 0
    req = urllib.request.Request(url, headers={"User-Agent": APP})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            header_size = resp.headers.get("Content-Length")
            if header_size:
                try:
                    declared_size = int(header_size)
                except (TypeError, ValueError) as exc:
                    raise InstallerError("HAOS server returned an invalid Content-Length") from exc
                if declared_size < 0 or declared_size > max_bytes:
                    raise InstallerError(f"HAOS image exceeds the configured {max_bytes} byte download limit")
                if expected_size is not None and declared_size != expected_size:
                    raise InstallerError(f"HAOS image size mismatch: expected {expected_size} bytes, server declared {declared_size}")
            partial_fd, partial_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=cache_dir)
            partial = Path(partial_name)
            with os.fdopen(partial_fd, "wb") as f:
                partial_fd = None
                while True:
                    chunk = resp.read(1024*1024)
                    if not chunk: break
                    total += len(chunk)
                    if total > max_bytes:
                        raise InstallerError(f"HAOS image exceeded the configured {max_bytes} byte download limit")
                    h.update(chunk); f.write(chunk)
        if expected_size is not None and total != expected_size:
            raise InstallerError(f"HAOS image size mismatch: expected {expected_size} bytes, got {total}")
        got = h.hexdigest()
        if got.lower() != sha256.lower():
            raise InstallerError(f"Image checksum mismatch: expected {sha256}, got {got}")
        os.replace(partial, dest)
        return dest
    finally:
        if partial_fd is not None:
            os.close(partial_fd)
        if partial is not None:
            partial.unlink(missing_ok=True)


@contextlib.contextmanager
def prepared_qcow_image(image: Path, cache_dir: Path, disk_gib: int):
    """Yield a validated, seekable QCOW2 temp file, removing it on exit."""
    listing = subprocess.run(["xz", "--robot", "--list", str(image)], check=True, capture_output=True, text=True)
    file_rows = [line.split("\t") for line in listing.stdout.splitlines() if line.startswith("file\t")]
    if len(file_rows) != 1 or len(file_rows[0]) < 5 or not file_rows[0][4].isdigit():
        raise InstallerError("Cannot determine the compressed image's expanded size; refusing to decompress")
    expanded_bytes = int(file_rows[0][4])
    expanded_limit = disk_gib * 1024**3 + 256 * 1024**2
    if expanded_bytes <= 0 or expanded_bytes > expanded_limit:
        raise InstallerError(f"Expanded image size ({expanded_bytes} bytes) exceeds the safe limit for a {disk_gib} GiB VM disk")
    free_bytes = os.statvfs(cache_dir).f_bavail * os.statvfs(cache_dir).f_frsize
    if free_bytes < expanded_bytes + 1024**3:
        raise InstallerError("Not enough free space in the image cache filesystem for safe decompression (including a 1 GiB reserve)")
    fd, temp_name = tempfile.mkstemp(prefix="haos-", suffix=".qcow2", dir=cache_dir)
    qcow = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            fd = -1
            subprocess.run(["xz", "-dc", "--", str(image)], stdout=out, check=True)
        if qcow.stat().st_size != expanded_bytes:
            raise InstallerError("Expanded HAOS image size differs from the XZ metadata")
        info_proc = subprocess.run(["qemu-img", "info", "--output=json", "-f", "qcow2", str(qcow)], check=True, capture_output=True, text=True)
        try:
            image_info = json.loads(info_proc.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise InstallerError("qemu-img returned invalid QCOW2 image information") from exc
        if image_info.get("format") != "qcow2":
            raise InstallerError("Downloaded HAOS image is not QCOW2 format")
        if image_info.get("backing-filename"):
            raise InstallerError("Refusing QCOW2 images that depend on a backing file")
        virtual_size = int(image_info.get("virtual-size") or 0)
        if virtual_size <= 0:
            raise InstallerError("QCOW2 image reports an invalid virtual size")
        if virtual_size > disk_gib * 1024**3:
            raise InstallerError(f"HAOS image needs {virtual_size/1024**3:.1f} GiB but the planned zvol is only {disk_gib} GiB")
        yield qcow
    finally:
        if fd >= 0:
            os.close(fd)
        qcow.unlink(missing_ok=True)


def convert_qcow_to_zvol(qcow: Path, zvol_dev: str):
    run_checked(["qemu-img", "convert", "-p", "-f", "qcow2", "-O", "raw", str(qcow), zvol_dev])

def run_checked(cmd: list[str], **kwargs):
    printable = " ".join(cmd[:3]) + (" ..." if len(cmd)>3 else "")
    print(f"Running: {printable}")
    subprocess.run(cmd, check=True, **kwargs)

def create_manifest(path: Path, plan: Plan) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"tool": APP, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "resources": [], "plan": dataclasses.asdict(plan)}
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise InstallerError(f"Task manifest already exists at {path}; choose a new manifest path") from exc
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(manifest, indent=2))
        f.flush()
        os.fsync(f.fileno())
    return manifest

def update_manifest(path: Path, manifest: dict):
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(manifest, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)

def install(client, plan: Plan, args):
    require_tools_for_install(False)
    cache_dir = Path(args.cache_dir).expanduser()
    image = download_and_verify(
        plan.image_url,
        plan.image_sha256,
        cache_dir,
        max_bytes=min(MAX_DOWNLOAD_BYTES_DEFAULT, args.max_download_gib * 1024**3),
        expected_size=plan.image_size or None,
    )
    manifest_path = Path(args.manifest or f"./haos-installer-{plan.vm_name}-manifest.json").resolve()
    parent = "/".join(plan.zvol.split("/")[:-1])
    dev = f"/dev/zvol/{plan.zvol}"
    # Download, checksum, decompression, and QCOW2 validation all happen before
    # creating any TrueNAS datasets, zvols, VMs, or devices.
    with prepared_qcow_image(image, cache_dir, plan.disk_gib) as qcow:
        manifest = create_manifest(manifest_path, plan)
        print(f"Task manifest: {manifest_path}")
        parent_matches = client.call("pool.dataset.query", [[["id","=",parent]]])
        if parent_matches:
            if any(d.get("type") != "FILESYSTEM" for d in parent_matches):
                raise InstallerError(f"Parent dataset {parent!r} exists but is not a filesystem; refusing to use it")
        else:
            client.call("pool.dataset.create", [{"name": parent, "type":"FILESYSTEM", "comments": f"Created by {APP} for HAOS VM disks"}])
            manifest["resources"].append({"type":"dataset", "id":parent}); update_manifest(manifest_path, manifest)
        client.call("pool.dataset.create", [{"name": plan.zvol, "type":"VOLUME", "volsize": plan.disk_gib * 1024**3, "comments": f"Created by {APP} for {plan.vm_name}"}])
        manifest["resources"].append({"type":"zvol", "id":plan.zvol, "dev":dev}); update_manifest(manifest_path, manifest)
        if not Path(dev).exists():
            raise InstallerError(f"Created zvol device path is not visible: {dev}")
        # Destination is only the newly created zvol recorded in the manifest.
        convert_qcow_to_zvol(qcow, dev)
    vm = client.call("vm.create", [{
        "name": plan.vm_name,
        "description": f"Home Assistant OS VM created by {APP} (BETA)",
        "vcpus": plan.vcpus,
        "cores": plan.vcpus,
        "threads": 1,
        "memory": plan.ram_mib,
        "min_memory": None,
        "cpu_mode": "HOST-MODEL",
        "bootloader": "UEFI",
        "bootloader_ovmf": "OVMF_CODE.fd",
        "trusted_platform_module": False,
        "autostart": plan.autostart,
        "ensure_display_device": True,
        "time": "UTC",
    }])
    vm_id = vm["id"]
    manifest["resources"].append({"type":"vm", "id":vm_id, "name":plan.vm_name}); update_manifest(manifest_path, manifest)
    disk = client.call("vm.device.create", [{"vm": vm_id, "attributes":{"dtype":"DISK", "path":dev, "type":"VIRTIO", "create_zvol":False, "iotype":"THREADS"}}])
    nic = client.call("vm.device.create", [{"vm": vm_id, "attributes":{"dtype":"NIC", "type":"VIRTIO", "nic_attach":plan.nic_attach, "mac": random_mac()}}])
    manifest["resources"].append({"type":"device", "id":disk["id"], "kind":"DISK"})
    manifest["resources"].append({"type":"device", "id":nic["id"], "kind":"NIC"}); update_manifest(manifest_path, manifest)
    if plan.start_vm:
        client.call("vm.start", [vm_id, {"overcommit": False}])
    print("\nCreated HAOS VM successfully.")
    print("Wait a few minutes, then visit http://homeassistant.local:8123/ or check your router/DHCP leases for the VM IP.")
    print(f"Keep this manifest for safe recovery/removal: {manifest_path}")

def random_mac() -> str:
    # Locally administered unicast MAC.
    octets = [0x02, random.randrange(0,256), random.randrange(0,256), random.randrange(0,256), random.randrange(0,256), random.randrange(0,256)]
    return ":".join(f"{x:02x}" for x in octets)

@contextlib.contextmanager
def lockfile(path: str):
    p = Path(path)
    fd = os.open(str(p), os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        p.unlink(missing_ok=True)

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="BETA guided HAOS installer for TrueNAS SCALE")
    ap.add_argument("--preview", action="store_true", help="Read-only preview. Refuses to create/modify resources.")
    ap.add_argument("--truenas-url", default=os.getenv("TRUENAS_URL", "http://127.0.0.1"), help="TrueNAS base URL or ws(s) API URL")
    ap.add_argument("--username", default=os.getenv("TRUENAS_USERNAME"))
    ap.add_argument("--password", default=os.getenv("TRUENAS_PASSWORD"))
    ap.add_argument("--api-key", default=os.getenv("TRUENAS_API_KEY"))
    ap.add_argument("--pool")
    ap.add_argument("--nic")
    ap.add_argument("--vm-name")
    ap.add_argument("--zvol-name")
    ap.add_argument("--vcpus", type=int)
    ap.add_argument("--ram-mib", type=int)
    ap.add_argument("--disk-gib", type=int)
    ap.add_argument("--test-limits", action="store_true", help="Clamp to live-test limits: 2 vCPU, 2 GiB RAM, 32 GiB disk")
    ap.add_argument("--image-url")
    ap.add_argument("--image-sha256")
    ap.add_argument("--no-start", action="store_true")
    ap.add_argument("--yes", action="store_true", help="Proceed without final interactive confirmation")
    ap.add_argument("--allow-unsupported-version", action="store_true", help="Allow unsupported versions in read-only preview only; live installs stay blocked.")
    ap.add_argument("--cache-dir", default="/tmp/haos-truenas-installer-cache")
    ap.add_argument("--manifest")
    ap.add_argument("--max-download-gib", type=int, default=10)
    ap.add_argument("--lock-file", default="/tmp/haos-truenas-installer.lock")
    return ap.parse_args(argv)

def use_local_midclt(args) -> bool:
    return (
        not args.api_key
        and not args.username
        and not args.password
        and args.truenas_url in ("http://127.0.0.1", "http://localhost", "127.0.0.1", "localhost")
        and shutil.which("midclt") is not None
    )

def main(argv=None):
    args = parse_args(argv)
    local_midclt = use_local_midclt(args)
    if not local_midclt and not args.preview and not args.api_key and not args.password:
        if not args.username:
            args.username = ask("TrueNAS username", "root")
        args.password = getpass.getpass("TrueNAS password: ")
    if not local_midclt and args.preview and not args.api_key and args.username and args.password is None:
        args.password = getpass.getpass("TrueNAS password (for read-only preview): ")
    with lockfile(args.lock_file):
        client = MidcltClient() if local_midclt else TrueNASClient(args.truenas_url, args.username, args.password, args.api_key)
        try:
            plan, meta = build_plan(client, args)
            print_plan(plan, meta)
            if plan.preview:
                print("\nPreview complete. No resources were created or modified.")
                return 0
            if not args.yes:
                confirm = ask("Type CREATE to create this NEW VM and zvol", "")
                if confirm != "CREATE":
                    raise InstallerError("Cancelled before any write operations")
            install(client, plan, args)
            return 0
        finally:
            client.close()

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileExistsError:
        print("Another installer run appears to be active (lock file exists).", file=sys.stderr)
        raise SystemExit(2)
    except InstallerError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
