#!/usr/bin/env python3
"""Guided Ubuntu Server cloud-image installer for TrueNAS SCALE.

BETA. Only creates a new, explicitly named VM and new installer-owned storage.
Preview mode performs read-only TrueNAS queries and no downloads or writes.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import random
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

APP = "ubuntu-server-truenas-installer"
SUPPORTED_VERSION = "TrueNAS-26.0.0-BETA.2"
IMAGE_NAME = "ubuntu-26.04-server-cloudimg-amd64.img"
IMAGE_URL = "https://cloud-images.ubuntu.com/releases/server/26.04/release-20260927/" + IMAGE_NAME
# SHA256 from Canonical's signed release-20260927/SHA256SUMS. The detached
# signature was verified against UEC Image Automatic Signing Key fingerprint
# D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81 when this revision was prepared.
IMAGE_SHA256 = "8800651811af9a85465ad1d552add729947bb16488dddb4a9b5305a3d97332b2"
IMAGE_MAX_BYTES = 4 * 1024**3
IMAGE_SIZE = 865115136
MIN_POOL_FREE_FRACTION = 0.20
DATASET_SUFFIX = "ubuntu-server-vms"
DATASET_COMMENT = f"Created by {APP} for Ubuntu Server VM disks"


class InstallerError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Plan:
    truenas_version: str
    pool: str
    parent_dataset: str
    zvol: str
    vm_name: str
    hostname: str
    username: str
    ssh_key_sha256: str
    nic_attach: str
    vcpus: int
    ram_mib: int
    disk_gib: int
    start_vm: bool


class MidcltClient:
    """Read/write TrueNAS API adapter for a root shell on the NAS host."""

    def __init__(self):
        if shutil.which("midclt") is None:
            raise InstallerError("midclt was not found; run this installer from the TrueNAS host shell")

    def call(self, method: str, params=None):
        command = ["midclt", "call", method]
        command.extend(json.dumps(param) for param in (params or []))
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise InstallerError(f"TrueNAS midclt {method} failed: {detail}")
        output = result.stdout.strip()
        if not output:
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            if method == "system.version" and re.fullmatch(r"TrueNAS-[A-Za-z0-9._+-]+", output):
                return output
            raise InstallerError(f"TrueNAS midclt {method} returned invalid JSON") from exc

    def close(self):
        return None


def validate_vm_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{1,63}", name or ""):
        raise InstallerError("VM name must be 1-63 ASCII letters or digits")
    return name


def validate_username(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", name or ""):
        raise InstallerError("Linux username must use lowercase letters, digits, underscores or hyphens and start with a letter or underscore")
    return name


def validate_hostname(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name or ""):
        raise InstallerError("Hostname must be 1-63 lowercase letters, digits or interior hyphens")
    return name


def validate_ssh_public_key(value: str) -> str:
    value = value.strip()
    if "\n" in value or "\r" in value or len(value) > 16384:
        raise InstallerError("Provide exactly one SSH public key line")
    pieces = value.split()
    allowed = {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"}
    if len(pieces) < 2 or pieces[0] not in allowed:
        raise InstallerError("Unsupported SSH key; provide one OpenSSH ed25519, RSA, ECDSA, or supported security-key public key")
    try:
        blob = base64.b64decode(pieces[1], validate=True)
        key_type_length = int.from_bytes(blob[:4], "big")
        embedded_type = blob[4:4 + key_type_length].decode("ascii")
    except (ValueError, UnicodeDecodeError, IndexError) as exc:
        raise InstallerError("SSH public key is malformed") from exc
    if not blob or embedded_type != pieces[0]:
        raise InstallerError("SSH public key type does not match its encoded key data")
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in " ".join(pieces[2:])):
        raise InstallerError("SSH key comment must contain printable ASCII only")
    return " ".join(pieces)


def validate_pool_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name or ""):
        raise InstallerError("Invalid pool name")
    return name


def dataset_comment(dataset: dict) -> str:
    props = dataset.get("user_properties") or {}
    comment = props.get("comments", dataset.get("comments", ""))
    if isinstance(comment, dict):
        return str(comment.get("parsed") or comment.get("value") or comment.get("rawvalue") or "")
    return str(comment or "")


def select_pool(pools: list[dict]) -> str:
    healthy = [p for p in pools if p.get("healthy") and p.get("status") == "ONLINE"]
    if not healthy:
        raise InstallerError("No healthy ONLINE storage pools are available")
    print("\nChoose the destination pool; no pool is selected automatically:")
    for index, pool in enumerate(healthy, 1):
        free, size = int(pool.get("free") or 0), int(pool.get("size") or 0)
        pct = 100 * free / size if size else 0
        print(f"  {index}. {pool['name']} — {free / 1024**3:.0f} GiB free of {size / 1024**3:.0f} GiB ({pct:.1f}% free)")
    while True:
        value = input("Choose pool number: ").strip()
        if value.isdigit() and 1 <= int(value) <= len(healthy):
            return healthy[int(value) - 1]["name"]
        print("Enter one of the listed pool numbers.")


def select_item(label: str, choices: list[str]) -> str:
    if not choices:
        raise InstallerError(f"No existing TrueNAS choices are available for {label}")
    print(f"\nChoose {label}:")
    for i, item in enumerate(choices, 1):
        print(f"  {i}. {item}")
    while True:
        value = input(f"Choose {label} number: ").strip()
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1]
        print("Enter one of the listed numbers.")


def ask_int(prompt: str, default: int, minimum: int, maximum: int) -> int:
    while True:
        value = input(f"{prompt} [{default}]: ").strip() or str(default)
        try:
            number = int(value)
        except ValueError:
            print("Enter a whole number.")
            continue
        if minimum <= number <= maximum:
            return number
        print(f"Enter a value from {minimum} to {maximum}.")


def validate_api_methods(methods: dict) -> None:
    required = {
        "system.info", "system.version", "core.get_methods", "pool.query",
        "pool.dataset.query", "pool.dataset.create", "vm.query", "vm.create",
        "vm.start", "vm.device.query", "vm.device.create",
        "vm.device.nic_attach_choices",
    }
    missing = sorted(required - set(methods))
    if missing:
        raise InstallerError("Required TrueNAS methods are missing: " + ", ".join(missing))
    # Fail closed if the installed middleware schema does not expose the CD-ROM
    # and storage attributes this installer must send. This is only a schema
    # query; it does not create a trial device.
    device_schema = json.dumps(methods.get("vm.device.create"), sort_keys=True)
    vm_schema = json.dumps(methods.get("vm.create"), sort_keys=True)
    for token in ("CDROM", "DISK", "NIC", "path", "dtype", "order"):
        if token not in device_schema:
            raise InstallerError(f"TrueNAS vm.device.create schema does not advertise required {token!r} support")
    for token in ("bootloader", "UEFI", "memory", "vcpus", "autostart", "ensure_display_device"):
        if token not in vm_schema:
            raise InstallerError(f"TrueNAS vm.create schema does not advertise required {token!r} support")


def build_plan(client, args, ssh_public_key: str | None = None) -> Plan:
    version = client.call("system.version")
    info = client.call("system.info") or {}
    methods = client.call("core.get_methods")
    if not isinstance(methods, dict):
        raise InstallerError("TrueNAS did not return an API method schema")
    validate_api_methods(methods)
    if version != SUPPORTED_VERSION:
        if not (args.preview and args.allow_unsupported_version):
            raise InstallerError(f"Unsupported TrueNAS build {version}; live installation is gated to {SUPPORTED_VERSION}")
    pools = client.call("pool.query") or []
    healthy = [p for p in pools if p.get("healthy") and p.get("status") == "ONLINE"]
    if not healthy:
        raise InstallerError("No healthy ONLINE storage pools are available")
    pool_name = validate_pool_name(args.pool or select_pool(healthy))
    pool = next((p for p in healthy if p.get("name") == pool_name), None)
    if pool is None:
        raise InstallerError(f"Pool {pool_name!r} is not healthy and ONLINE")

    nic_info = client.call("vm.device.nic_attach_choices") or {}
    nics = list(dict.fromkeys((nic_info.get("BRIDGE") or []) + (nic_info.get("MACVLAN") or [])))
    nic = args.nic or select_item("existing VM network attachment", nics)
    if nic not in nics:
        raise InstallerError(f"Network attachment {nic!r} is not a current TrueNAS choice")

    vm_name = validate_vm_name(args.vm_name or input("VM name [UBUNTUSERVER]: ").strip() or "UBUNTUSERVER")
    hostname = validate_hostname(args.hostname or input("Linux hostname [ubuntu-server]: ").strip() or "ubuntu-server")
    username = validate_username(args.username or input("Linux admin username [ubuntuadmin]: ").strip() or "ubuntuadmin")
    if args.test_limits:
        print("Test limits active: at most 2 vCPU, 2048 MiB RAM, and 32 GiB disk.")
        vcpus = min(args.vcpus or 2, 2)
        ram_mib = min(args.ram_mib or 2048, 2048)
        disk_gib = min(args.disk_gib or 32, 32)
    else:
        vcpus = args.vcpus if args.vcpus is not None else ask_int("vCPU cores", 2, 1, 16)
        ram_mib = args.ram_mib if args.ram_mib is not None else ask_int("RAM MiB", 4096, 2048, 65536)
        disk_gib = args.disk_gib if args.disk_gib is not None else ask_int("Virtual disk GiB", 64, 16, 1024)
    if not (1 <= vcpus <= 16 and 2048 <= ram_mib <= 65536 and 16 <= disk_gib <= 1024):
        raise InstallerError("Requested CPU, RAM, or disk size is outside supported bounds")

    existing_vms = client.call("vm.query") or []
    if any(vm.get("name") == vm_name for vm in existing_vms):
        raise InstallerError(f"A VM named {vm_name!r} already exists; refusing to overwrite")
    parent = f"{pool_name}/{DATASET_SUFFIX}"
    zvol = f"{parent}/{vm_name.lower()}"
    parent_rows = client.call("pool.dataset.query", [[ ["id", "=", parent] ]]) or []
    if parent_rows and (len(parent_rows) != 1 or parent_rows[0].get("type") != "FILESYSTEM" or dataset_comment(parent_rows[0]) != DATASET_COMMENT):
        raise InstallerError(f"Dataset {parent!r} already exists without this installer's ownership marker; refusing to use it")
    if client.call("pool.dataset.query", [[ ["id", "=", zvol] ]]):
        raise InstallerError(f"Zvol/dataset {zvol!r} already exists; refusing to overwrite")
    if any((d.get("attributes") or {}).get("path") == f"/dev/zvol/{zvol}" for d in (client.call("vm.device.query") or [])):
        raise InstallerError("The planned zvol path is already attached to a VM device")
    required_bytes = disk_gib * 1024**3 + 64 * 1024**2
    after = int(pool.get("free") or 0) - required_bytes
    if after < int(pool.get("size") or 0) * MIN_POOL_FREE_FRACTION:
        raise InstallerError("Planned zvol would leave less than 20% of the selected pool free")
    total_ram = int(info.get("physmem") or 0)
    if total_ram and ram_mib * 1024**2 > total_ram * 0.7:
        raise InstallerError("Requested VM RAM exceeds 70% of physical RAM; reduce it and preserve host/application headroom")
    if total_ram:
        running_vm_ram = 0
        for existing in existing_vms:
            status = existing.get("status")
            state = status if isinstance(status, str) else (status.get("state") if isinstance(status, dict) else None)
            if state == "RUNNING":
                running_vm_ram += int(existing.get("memory") or 0) * 1024**2
        if running_vm_ram + ram_mib * 1024**2 > total_ram * 0.8:
            raise InstallerError("Running VM allocations plus the planned VM exceed the 80% host-memory safety ceiling")

    key_hash = "not supplied in preview" if not ssh_public_key else hashlib.sha256(ssh_public_key.encode()).hexdigest()
    return Plan(version, pool_name, parent, zvol, vm_name, hostname, username, key_hash, nic, vcpus, ram_mib, disk_gib, bool(args.start))


def print_plan(plan: Plan, preview: bool) -> None:
    suffix = " (PREVIEW — no changes or downloads)" if preview else ""
    print("\nUbuntu Server installation plan" + suffix)
    print(f"  TrueNAS build: {plan.truenas_version}")
    print(f"  Ubuntu image: Ubuntu Server 26.04 LTS cloud image (amd64), pinned release 20260927")
    print(f"  Image SHA-256: {IMAGE_SHA256}")
    print(f"  Pool: {plan.pool}")
    print(f"  Parent dataset: {plan.parent_dataset}")
    print(f"  New zvol: {plan.zvol} ({plan.disk_gib} GiB)")
    print(f"  VM: {plan.vm_name} — {plan.vcpus} vCPU, {plan.ram_mib} MiB RAM")
    print(f"  Linux hostname/user: {plan.hostname}/{plan.username}")
    print(f"  SSH public-key SHA-256: {plan.ssh_key_sha256}")
    print(f"  NIC: existing attachment {plan.nic_attach}")
    print("  Firmware: UEFI; Secure Boot disabled")
    print("  Cloud-init: unique NoCloud seed ISO; password SSH login disabled")
    print(f"  Start after creation: {plan.start_vm}; autostart: disabled")


def ensure_trusted_directory_chain(directory: Path, create: bool = False) -> Path:
    directory = Path(os.path.abspath(os.fspath(directory)))
    owners = {0, os.geteuid()}
    chain = [*reversed(directory.parents), directory]
    for path in chain:
        if create:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners:
            raise InstallerError("Cache/manifest path contains a symlink, non-directory, or untrusted owner")
        if info.st_mode & 0o022 and not (info.st_mode & stat.S_ISVTX and info.st_uid in owners):
            raise InstallerError("Cache/manifest path contains a group/world-writable ancestor without a trusted sticky bit")
    return directory


def ensure_private_cache(path: Path) -> Path:
    path = ensure_trusted_directory_chain(path.expanduser(), create=True)
    info = path.lstat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise InstallerError("Image cache must be owned by the current user and not group/world writable")
    os.chmod(path, 0o700)
    return path


def resolve_manifest(path: str | None, vm_name: str) -> Path:
    return Path(path).expanduser() if path else Path("/tmp") / f"ubuntu-server-installer-{vm_name}.json"


def create_manifest(path: Path, plan: Plan) -> dict:
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    ensure_trusted_directory_chain(path.parent, create=True)
    manifest: dict[str, Any] = {
        "tool": APP,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "resources": [],
        "plan": dataclasses.asdict(plan),
        "image": {"url": IMAGE_URL, "sha256": IMAGE_SHA256, "name": IMAGE_NAME},
    }
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        raise InstallerError(f"Manifest already exists at {path}; choose a new --manifest path") from exc
    except OSError as exc:
        raise InstallerError(f"Cannot create private task manifest at {path}") from exc
    with os.fdopen(fd, "w") as stream:
        json.dump(manifest, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return manifest


def save_manifest(path: Path, manifest: dict) -> None:
    ensure_trusted_directory_chain(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(manifest, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def download_image(cache_dir: Path, max_bytes: int = IMAGE_MAX_BYTES) -> Path:
    if urllib.parse.urlparse(IMAGE_URL).scheme != "https":
        raise InstallerError("Ubuntu image source must use HTTPS")
    cache_dir = ensure_private_cache(cache_dir)
    target = cache_dir / f"ubuntu-26.04-{IMAGE_SHA256}.img"
    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None
    if info:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise InstallerError("Refusing unsafe or unowned cached Ubuntu image")
        if info.st_size == IMAGE_SIZE and file_sha256(target) == IMAGE_SHA256:
            return target
        target.unlink()
    statvfs = os.statvfs(cache_dir)
    free = statvfs.f_bavail * statvfs.f_frsize
    if free < IMAGE_SIZE + 1024**3:
        raise InstallerError("Image cache needs the Ubuntu image size plus 1 GiB free-space reserve")
    opener = urllib.request.build_opener(HTTPSRedirects())
    temporary = None
    try:
        request = urllib.request.Request(IMAGE_URL, headers={"User-Agent": APP})
        with opener.open(request, timeout=120) as response:
            if urllib.parse.urlparse(response.geturl()).scheme != "https":
                raise InstallerError("Ubuntu image download left HTTPS")
            length = response.headers.get("Content-Length")
            if length and int(length) != IMAGE_SIZE:
                raise InstallerError(f"Ubuntu image length mismatch: expected {IMAGE_SIZE}, got {length}")
            fd, name = tempfile.mkstemp(prefix="ubuntu-image-", suffix=".part", dir=cache_dir)
            temporary = Path(name)
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > min(max_bytes, IMAGE_SIZE):
                        raise InstallerError("Ubuntu image exceeded the pinned download-size limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if total != IMAGE_SIZE:
            raise InstallerError(f"Ubuntu image length mismatch: expected {IMAGE_SIZE}, got {total}")
        if digest.hexdigest() != IMAGE_SHA256:
            raise InstallerError("Ubuntu image SHA-256 mismatch; refusing to use it")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        temporary = None
        return target
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


class HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme.lower() != "https":
            raise InstallerError("Refusing an HTTPS-to-HTTP redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_image(image: Path, disk_gib: int) -> dict:
    try:
        result = subprocess.run(["qemu-img", "info", "--output=json", str(image)], check=True, capture_output=True, text=True)
        info = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise InstallerError("Could not inspect the verified Ubuntu image with qemu-img") from exc
    if info.get("format") != "qcow2" or info.get("backing-filename"):
        raise InstallerError("Expected a standalone QCOW2 Ubuntu cloud image with no backing file")
    virtual_size = int(info.get("virtual-size") or 0)
    if virtual_size <= 0 or virtual_size > disk_gib * 1024**3:
        raise InstallerError("Ubuntu image virtual size is invalid or larger than the planned zvol")
    return info


def cloud_config(hostname: str, username: str, public_key: str) -> tuple[str, str]:
    # JSON string literals are valid YAML scalar values and avoid user-data
    # injection via username, hostname, or the public-key comment.
    user_data = """#cloud-config\n"""
    user_data += f"hostname: {json.dumps(hostname)}\nmanage_etc_hosts: true\n"
    user_data += "users:\n  - default\n"
    user_data += f"  - name: {json.dumps(username)}\n"
    user_data += "    groups: [adm, sudo]\n    shell: /bin/bash\n    lock_passwd: true\n"
    user_data += "    sudo: ALL=(ALL) NOPASSWD:ALL\n    ssh_authorized_keys:\n"
    user_data += f"      - {json.dumps(public_key)}\n"
    user_data += "ssh_pwauth: false\ndisable_root: true\n"
    meta_data = f"instance-id: {json.dumps('truenas-' + uuid.uuid4().hex)}\nlocal-hostname: {json.dumps(hostname)}\n"
    return user_data, meta_data


def create_seed_iso(seed_dir: Path, output_iso: Path) -> None:
    xorriso = shutil.which("xorriso")
    if xorriso:
        command = [xorriso, "-as", "mkisofs", "-quiet", "-volid", "CIDATA", "-o", str(output_iso), "-J", "-r", str(seed_dir)]
        subprocess.run(command, check=True, capture_output=True, text=True)
    else:
        genisoimage = shutil.which("genisoimage") or shutil.which("mkisofs")
        if not genisoimage:
            raise InstallerError("Live install requires xorriso, genisoimage, or mkisofs to create the cloud-init seed ISO; no packages are installed automatically")
        command = [genisoimage, "-quiet", "-volid", "CIDATA", "-output", str(output_iso), "-joliet", "-rock", str(seed_dir / "user-data"), str(seed_dir / "meta-data")]
        subprocess.run(command, check=True, capture_output=True, text=True)
    if not output_iso.is_file() or output_iso.stat().st_size < 2048:
        raise InstallerError("Cloud-init seed ISO generator did not create a valid-sized ISO")
    os.chmod(output_iso, 0o600)


def validate_mountpoint(path: Path) -> Path:
    if not path.is_absolute() or not str(path).startswith("/mnt/"):
        raise InstallerError("Installer dataset mountpoint must be inside /mnt")
    current = Path("/mnt")
    try:
        base_info = current.lstat()
        if not stat.S_ISDIR(base_info.st_mode) or base_info.st_uid != 0 or base_info.st_mode & 0o022:
            raise InstallerError("TrueNAS /mnt mountpoint is not a trusted root-owned directory")
        parts = path.relative_to("/mnt").parts
        if any(part in ("", ".", "..") for part in parts):
            raise InstallerError("Installer dataset mountpoint contains an unsafe path component")
        for part in parts:
            current = current / part
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise InstallerError("Installer dataset mountpoint contains a symlink, non-directory, or writable untrusted path")
    except OSError as exc:
        raise InstallerError("Installer dataset mountpoint is not accessible") from exc
    return path


def random_mac() -> str:
    octets = [0x02, random.randrange(256), random.randrange(256), random.randrange(256), random.randrange(256), random.randrange(256)]
    return ":".join(f"{octet:02x}" for octet in octets)


def install(client, plan: Plan, args, public_key: str) -> Path:
    if os.geteuid() != 0:
        raise InstallerError("Live install requires a root TrueNAS shell to write to /dev/zvol; no downloads or TrueNAS writes were made")
    if not (shutil.which("qemu-img") and (shutil.which("xorriso") or shutil.which("genisoimage") or shutil.which("mkisofs"))):
        raise InstallerError("Live install requires qemu-img and xorriso (or genisoimage/mkisofs); no packages are installed automatically")

    manifest_path = resolve_manifest(args.manifest, plan.vm_name)
    manifest = create_manifest(manifest_path, plan)
    print(f"Task manifest: {manifest_path}")
    cache_dir = ensure_private_cache(Path(args.cache_dir))
    image = download_image(cache_dir, max_bytes=args.max_download_gib * 1024**3)
    inspect_image(image, plan.disk_gib)

    with tempfile.TemporaryDirectory(prefix="ubuntu-cloud-init-", dir=cache_dir) as temp_name:
        work = Path(temp_name)
        seed_dir = work / "seed"
        seed_dir.mkdir(mode=0o700)
        user_data, meta_data = cloud_config(plan.hostname, plan.username, public_key)
        for name, text in (("user-data", user_data), ("meta-data", meta_data)):
            file_path = seed_dir / name
            fd = os.open(file_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        seed_iso_tmp = work / "seed.iso"
        create_seed_iso(seed_dir, seed_iso_tmp)

        # Repeat name/path checks immediately before the first TrueNAS write;
        # plans can be stale by the time a user confirms them.
        if any(vm.get("name") == plan.vm_name for vm in (client.call("vm.query") or [])):
            raise InstallerError("A VM with this name appeared after planning; refusing to continue")
        if client.call("pool.dataset.query", [[["id", "=", plan.zvol]]]):
            raise InstallerError("The planned zvol appeared after planning; refusing to overwrite it")
        if any((d.get("attributes") or {}).get("path") == f"/dev/zvol/{plan.zvol}" for d in (client.call("vm.device.query") or [])):
            raise InstallerError("The planned zvol became attached after planning; refusing to write it")
        parent_rows = client.call("pool.dataset.query", [[["id", "=", plan.parent_dataset]]]) or []
        parent_created = False
        if not parent_rows:
            planned = {"type": "dataset", "id": plan.parent_dataset}
            manifest["resources"].append(planned)
            save_manifest(manifest_path, manifest)
            created = client.call("pool.dataset.create", [{"name": plan.parent_dataset, "type": "FILESYSTEM", "comments": DATASET_COMMENT}])
            parent_created = True
            planned["created"] = True
            planned["middleware_id"] = created.get("id") if isinstance(created, dict) else None
            save_manifest(manifest_path, manifest)
            parent_rows = client.call("pool.dataset.query", [[["id", "=", plan.parent_dataset]]]) or []
        if len(parent_rows) != 1 or parent_rows[0].get("type") != "FILESYSTEM" or dataset_comment(parent_rows[0]) != DATASET_COMMENT:
            raise InstallerError("Installer parent dataset could not be verified after creation")
        mountpoint = validate_mountpoint(Path(parent_rows[0].get("mountpoint") or ""))
        if parent_created:
            os.chmod(mountpoint, 0o755)
        seed_path = mountpoint / f"{plan.vm_name.lower()}-cloud-init.iso"
        if seed_path.exists() or seed_path.is_symlink():
            raise InstallerError("The planned cloud-init seed file already exists; refusing to overwrite")
        seed_resource: dict[str, Any] = {"type": "file", "path": str(seed_path), "role": "cloud-init-seed"}
        manifest["resources"].append(seed_resource)
        save_manifest(manifest_path, manifest)
        fd = os.open(seed_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as output, open(seed_iso_tmp, "rb") as source:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        if file_sha256(seed_path) != file_sha256(seed_iso_tmp):
            raise InstallerError("Copied cloud-init seed ISO failed its integrity check")
        # It contains only public SSH key material; world-readable mode is
        # needed for the unprivileged TrueNAS VM process to read the CD-ROM.
        os.chmod(seed_path, 0o644)
        seed_resource["created"] = True
        save_manifest(manifest_path, manifest)

        zvol = plan.zvol
        # Recheck just before creating the zvol, then verify its type, size,
        # ownership marker and lack of attachments before writing the image.
        if client.call("pool.dataset.query", [[["id", "=", plan.zvol]]]):
            raise InstallerError("The planned zvol appeared before creation; refusing to overwrite it")
        planned_zvol = {"type": "zvol", "id": zvol, "dev": f"/dev/zvol/{zvol}"}
        manifest["resources"].append(planned_zvol)
        save_manifest(manifest_path, manifest)
        created_zvol = client.call("pool.dataset.create", [{"name": zvol, "type": "VOLUME", "volsize": plan.disk_gib * 1024**3, "comments": f"Created by {APP} for {plan.vm_name}"}])
        planned_zvol["created"] = True
        planned_zvol["middleware_id"] = created_zvol.get("id") if isinstance(created_zvol, dict) else None
        save_manifest(manifest_path, manifest)
        zvol_rows = client.call("pool.dataset.query", [[["id", "=", zvol]]]) or []
        if len(zvol_rows) != 1 or zvol_rows[0].get("type") != "VOLUME":
            raise InstallerError("New zvol is missing or has an unexpected type; refusing to write the image")
        volume_size = zvol_rows[0].get("volsize")
        if isinstance(volume_size, dict):
            volume_size = volume_size.get("parsed")
        if int(volume_size or 0) != plan.disk_gib * 1024**3 or dataset_comment(zvol_rows[0]) != f"Created by {APP} for {plan.vm_name}":
            raise InstallerError("New zvol size or ownership marker does not match the plan; refusing to write the image")
        if any((d.get("attributes") or {}).get("path") == f"/dev/zvol/{zvol}" for d in (client.call("vm.device.query") or [])):
            raise InstallerError("New zvol is already attached to a VM device; refusing to write the image")
        zvol_device = f"/dev/zvol/{zvol}"
        if not Path(zvol_device).exists() or not os.access(zvol_device, os.W_OK):
            raise InstallerError(f"New zvol device is not writable: {zvol_device}; resources remain in {manifest_path}")
        try:
            subprocess.run(["qemu-img", "convert", "-p", "-f", "qcow2", "-O", "raw", str(image), zvol_device], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise InstallerError(f"Image conversion failed; keep the manifest and do not rerun fresh: {manifest_path}") from exc

        vm_data = {
            "name": plan.vm_name,
            "description": f"Ubuntu Server 26.04 LTS created by {APP} (BETA)",
            "vcpus": plan.vcpus,
            "cores": plan.vcpus,
            "threads": 1,
            "memory": plan.ram_mib,
            "min_memory": None,
            "cpu_mode": "HOST-MODEL",
            "bootloader": "UEFI",
            "bootloader_ovmf": "OVMF_CODE.fd",
            "trusted_platform_module": False,
            "autostart": False,
            "ensure_display_device": True,
            "time": "UTC",
        }
        if any(existing.get("name") == plan.vm_name for existing in (client.call("vm.query") or [])):
            raise InstallerError("A VM with this name appeared during image preparation; storage is preserved and no VM was created")
        vm = client.call("vm.create", [vm_data])
        if not isinstance(vm, dict) or not isinstance(vm.get("id"), int):
            raise InstallerError(f"TrueNAS did not return the new VM ID; inspect the manifest before continuing: {manifest_path}")
        vm_id = vm["id"]
        manifest["resources"].append({"type": "vm", "id": vm_id, "name": plan.vm_name})
        save_manifest(manifest_path, manifest)

        devices = [
            ("DISK", {"dtype": "DISK", "path": zvol_device, "type": "VIRTIO", "create_zvol": False, "iotype": "THREADS"}, 1000),
            ("NIC", {"dtype": "NIC", "type": "VIRTIO", "nic_attach": plan.nic_attach, "mac": random_mac()}, 2000),
            ("CDROM", {"dtype": "CDROM", "path": str(seed_path)}, 3000),
        ]
        for kind, attributes, order in devices:
            device = client.call("vm.device.create", [{"vm": vm_id, "order": order, "attributes": attributes}])
            if not isinstance(device, dict) or not isinstance(device.get("id"), int):
                raise InstallerError(f"TrueNAS did not return the new {kind} device ID; inspect the manifest before continuing: {manifest_path}")
            manifest["resources"].append({"type": "device", "id": device["id"], "kind": kind})
            save_manifest(manifest_path, manifest)

        if plan.start_vm:
            client.call("vm.start", [vm_id, {"overcommit": False}])
        print("\nUbuntu Server VM created. No existing VM or host network configuration was changed.")
        if plan.start_vm:
            print("The VM was started. Open its TrueNAS console to observe first boot/cloud-init.")
        else:
            print("The VM remains powered off (--start was not selected). Start it in TrueNAS and watch its console for first boot/cloud-init.")
        print(f"Cloud-init created user {plan.username} with SSH-key-only authentication; SSH password login is disabled.")
        print("Use the VM's DHCP address with the matching private key from your own computer; the private key is never collected by this installer.")
        print(f"Keep the manifest for recovery: {manifest_path}")
        return manifest_path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="BETA Ubuntu Server 26.04 cloud-image installer for TrueNAS SCALE")
    parser.add_argument("--preview", action="store_true", help="Read-only TrueNAS plan; does not download images or write resources")
    parser.add_argument("--pool")
    parser.add_argument("--nic")
    parser.add_argument("--vm-name")
    parser.add_argument("--hostname")
    parser.add_argument("--username")
    parser.add_argument("--ssh-public-key-file", help="Path to the SSH public key on the TrueNAS host; public key only")
    parser.add_argument("--vcpus", type=int)
    parser.add_argument("--ram-mib", type=int)
    parser.add_argument("--disk-gib", type=int)
    parser.add_argument("--test-limits", action="store_true", help="Clamp to a small disposable-test plan: 2 vCPU, 2 GiB RAM, 32 GiB disk")
    parser.add_argument("--start", action="store_true", help="Start the new VM after creation; default leaves it powered off")
    parser.add_argument("--yes", action="store_true", help="Skip the final CREATE confirmation")
    parser.add_argument("--allow-unsupported-version", action="store_true", help="Permit unsupported versions in preview only")
    parser.add_argument("--manifest")
    parser.add_argument("--cache-dir", default=f"/tmp/ubuntu-server-installer-cache-{os.geteuid()}")
    parser.add_argument("--max-download-gib", type=int, default=4)
    parser.add_argument("--lock-file", default="/tmp/ubuntu-server-truenas-installer.lock")
    return parser.parse_args(argv)


def load_public_key(args, preview: bool) -> str | None:
    if args.ssh_public_key_file:
        path = Path(args.ssh_public_key_file).expanduser()
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 16384:
            raise InstallerError("SSH public-key file must be a regular, non-group/world-writable file no larger than 16 KiB")
        return validate_ssh_public_key(path.read_text(encoding="utf-8"))
    if preview:
        return None
    print("Paste one SSH public key line (public key only; never paste a private key).")
    return validate_ssh_public_key(input("SSH public key: "))


@contextlib.contextmanager
def lockfile(path: str):
    lock_path = Path(path)
    if lock_path.parent != Path("/tmp"):
        raise InstallerError("Lock file must be directly under /tmp")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        raise InstallerError("Another Ubuntu Server installer run appears active (lock file exists)") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not 1 <= args.max_download_gib <= 4:
        raise InstallerError("--max-download-gib must be between 1 and 4")
    public_key = load_public_key(args, args.preview)
    client = MidcltClient()
    try:
        plan = build_plan(client, args, public_key)
        print_plan(plan, args.preview)
        if args.preview:
            if not public_key:
                print("  SSH public key: required for live install; preview did not prompt for it")
            print("\nPreview complete. No downloads, manifests, or TrueNAS resources were created.")
            return 0
        if not public_key:
            raise InstallerError("A valid SSH public key is required before live installation")
        if os.geteuid() != 0:
            raise InstallerError("Live installation requires a root TrueNAS shell; no download or resource write was performed")
        if not args.yes and input("Type CREATE to create only this new VM and its new storage: ").strip() != "CREATE":
            raise InstallerError("Cancelled before downloads or TrueNAS writes")
        with lockfile(args.lock_file):
            install(client, plan, args, public_key)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallerError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
