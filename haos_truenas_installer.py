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
import stat
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

def create_tls_context(ca_cert: str|None = None, insecure_tls: bool = False) -> ssl.SSLContext:
    ca_path = Path(ca_cert).expanduser() if ca_cert else None
    if ca_path is not None and (not ca_path.is_file() or ca_path.is_symlink()):
        raise InstallerError("The configured TrueNAS CA certificate must be an existing regular file, not a symlink")
    try:
        if insecure_tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        context = ssl.create_default_context()
        if ca_path is not None:
            context.load_verify_locations(cafile=str(ca_path))
        return context
    except (OSError, ssl.SSLError) as exc:
        raise InstallerError("Could not initialize TLS verification for the TrueNAS connection") from exc

def websocket_client_supports_hostname_verification() -> bool:
    version = getattr(websocket, "__version__", "") if websocket is not None else ""
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", version) if isinstance(version, str) else None
    return bool(match and tuple(int(part or 0) for part in match.groups()) >= (0, 18, 0))

class TrueNASClient:
    def __init__(self, url: str, username: str|None, password: str|None, api_key: str|None, timeout: int = 60, tls_ca_cert: str|None = None, insecure_tls: bool = False):
        self.url = normalize_ws_url(url)
        self.username = username
        self.password = password
        self.api_key = api_key
        self.timeout = timeout
        self._id = 0
        secure = self.url.startswith("wss://")
        if (tls_ca_cert or insecure_tls) and not secure:
            raise InstallerError("--tls-ca-cert and --insecure-tls apply only to a wss:// TrueNAS URL")
        if insecure_tls:
            print("WARNING: --insecure-tls disables certificate and hostname verification; use --tls-ca-cert instead when possible.", file=sys.stderr)
        tls_context = create_tls_context(tls_ca_cert, insecure_tls) if secure else None
        if self.url.startswith("ws://"):
            host = urllib.parse.urlparse(self.url).hostname
            if host not in ("localhost", "127.0.0.1", "::1"):
                print("WARNING: remote ws:// sends TrueNAS credentials and API traffic without TLS; use an HTTPS/WSS URL with a trusted certificate where possible.", file=sys.stderr)
        use_websocket_client = websocket is not None
        if secure and not insecure_tls and use_websocket_client and not websocket_client_supports_hostname_verification():
            if ws_sync_connect is None:
                raise InstallerError("Secure remote connections with websocket-client require version 0.18.0 or newer for hostname verification; upgrade it or use the websockets sync adapter")
            use_websocket_client = False
        if use_websocket_client:
            assert websocket is not None
            if secure:
                sslopt = {"cert_reqs": ssl.CERT_NONE if insecure_tls else ssl.CERT_REQUIRED, "check_hostname": not insecure_tls}
                if tls_ca_cert and not insecure_tls:
                    sslopt["ca_certs"] = str(Path(tls_ca_cert).expanduser())
                self.ws = websocket.create_connection(self.url, timeout=timeout, sslopt=sslopt)
            else:
                self.ws = websocket.create_connection(self.url, timeout=timeout)
            self._send = self.ws.send
            self._recv = self.ws.recv
        elif ws_sync_connect is not None:
            if secure:
                self.ws = ws_sync_connect(self.url, open_timeout=timeout, max_size=64_000_000, ssl=tls_context)
            else:
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
        if value < min_value:
            print(f"Enter a value of at least {min_value}."); continue
        if max_value is not None and value > max_value:
            print(f"Enter a value no greater than {max_value}."); continue
        return value

def validate_name(name: str, what: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
        raise InstallerError(f"Invalid {what} name. Use 1-63 letters, numbers, dots, underscores or hyphens, starting with a letter/number.")
    return name

def validate_vm_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{1,63}", name):
        raise InstallerError("Invalid VM name. TrueNAS accepts only 1-63 ASCII letters and numbers for VM names.")
    return name

def ensure_trusted_directory_chain(directory: Path, purpose: str, create: bool = False) -> Path:
    """Reject symlinks and directory ancestors an untrusted local user can change."""
    directory = Path(os.path.abspath(os.fspath(directory)))
    trusted_owners = {0, os.geteuid()}
    chain = [*reversed(directory.parents), directory]
    try:
        for current in chain:
            if create:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise InstallerError(f"{purpose} path contains a symlink or non-directory component")
            if info.st_uid not in trusted_owners:
                raise InstallerError(f"{purpose} path contains a directory not owned by root or the current user")
            if info.st_mode & 0o022 and not (info.st_mode & stat.S_ISVTX and info.st_uid in trusted_owners):
                raise InstallerError(f"{purpose} path contains a group/world-writable directory without a trusted sticky bit")
    except InstallerError:
        raise
    except OSError as exc:
        raise InstallerError(f"Could not verify the {purpose.lower()} directory path") from exc
    return directory

def dataset_comment(dataset: dict) -> str:
    properties = dataset.get("user_properties") or {}
    comment = properties.get("comments", dataset.get("comments", ""))
    if isinstance(comment, dict):
        return str(comment.get("parsed") or comment.get("value") or comment.get("rawvalue") or "")
    return str(comment or "")

def version_supported(version: str) -> bool:
    return any(p.match(version) for p in SUPPORTED_VERSION_PATTERNS)

class HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme.lower() != "https":
            raise InstallerError("Refusing a download redirect that leaves HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def open_https_url(req, timeout: int):
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment or any(ch.isspace() for ch in url):
        raise InstallerError("Network downloads must use a valid HTTPS URL without embedded credentials or a fragment")
    return urllib.request.build_opener(HTTPSOnlyRedirectHandler()).open(req, timeout=timeout)

def fetch_latest_haos_asset() -> dict:
    req = urllib.request.Request(HAOS_REPO_API, headers={"Accept":"application/vnd.github+json", "User-Agent":APP})
    with open_https_url(req, timeout=60) as resp:
        if urllib.parse.urlparse(resp.geturl()).scheme.lower() != "https":
            raise InstallerError("HAOS release metadata did not remain on HTTPS")
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
    asset_url = asset.get("browser_download_url") or ""
    parsed_asset_url = urllib.parse.urlparse(asset_url)
    expected_asset_path = f"/home-assistant/operating-system/releases/download/{stable['tag_name']}/{asset.get('name', '')}"
    if (parsed_asset_url.scheme != "https" or parsed_asset_url.netloc != "github.com"
            or parsed_asset_url.path != expected_asset_path or parsed_asset_url.query or parsed_asset_url.fragment):
        raise InstallerError("HAOS release asset URL is not the expected HTTPS GitHub OVA release URL")
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise InstallerError("GitHub release asset does not provide a valid sha256 digest; refusing unverifiable image")
    try:
        asset_size = int(asset["size"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallerError("HAOS release asset has an invalid size") from exc
    if asset_size <= 0:
        raise InstallerError("HAOS release asset has an invalid size")
    return {"version": stable["tag_name"], "url": asset_url, "sha256": digest.split(":",1)[1], "size": asset_size, "name": asset["name"]}

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

def select_storage_pool(pools: list[dict]) -> str:
    if not pools:
        raise InstallerError("No healthy ONLINE pools are available")
    print("\nSelect the destination storage pool. No pool will be chosen automatically:")
    for idx, pool in enumerate(pools, 1):
        free = int(pool.get("free") or 0)
        size = int(pool.get("size") or 0)
        pct = free / size * 100 if size else 0
        print(f"  {idx}. {pool['name']} — {free / 1024**3:.0f} GiB free of {size / 1024**3:.0f} GiB ({pct:.1f}% free)")
    while True:
        raw = ask("Choose destination pool number", "")
        try:
            index = int(raw) - 1
            if 0 <= index < len(pools):
                return pools[index]["name"]
        except ValueError:
            pass
        print("Enter one of the listed pool numbers.")

def select_disk_size(default_gib: int = 64, test_limits: bool = False) -> int:
    if test_limits:
        print("Test limits are active: the VM disk size is fixed at 32 GiB.")
        return TEST_LIMITS["disk_gib"]
    sizes = [32, 64, 128, 256]
    print("\nChoose the HAOS virtual disk/zvol capacity (this does not select or repartition a physical drive):")
    for idx, size in enumerate(sizes, 1):
        suffix = " (recommended default)" if size == default_gib else (" (minimum)" if size == 32 else "")
        print(f"  {idx}. {size} GiB{suffix}")
    custom_index = len(sizes) + 1
    print(f"  {custom_index}. Custom size")
    default_index = sizes.index(default_gib) + 1 if default_gib in sizes else custom_index
    while True:
        raw = ask("Choose virtual disk size", str(default_index))
        try:
            choice = int(raw)
        except ValueError:
            choice = 0
        if 1 <= choice <= len(sizes):
            return sizes[choice - 1]
        if choice == custom_index:
            return ask_int("Custom disk size GiB", default_gib, 32)
        print("Enter one of the listed disk-size choices.")

def build_resume_plan(client, args, info: dict, version: str, required: list[str]) -> tuple[Plan, dict]:
    if args.manifest:
        raise InstallerError("Use only --resume-manifest when resuming; --manifest cannot be combined")
    path = Path(args.resume_manifest).expanduser()
    try:
        data = read_manifest_file(path)
        if not isinstance(data, dict) or data.get("tool") != APP or not isinstance(data.get("plan"), dict):
            raise ValueError("unrecognized manifest")
        plan = Plan(**data["plan"])
        manifest_vm_name = plan.vm_name
        if not isinstance(manifest_vm_name, str) or not manifest_vm_name or len(manifest_vm_name) > 63:
            raise ValueError("invalid saved VM name")
        if args.vm_name:
            plan = dataclasses.replace(plan, vm_name=validate_vm_name(args.vm_name))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise InstallerError(f"Cannot read a valid installer manifest at {path}") from exc
    if plan.preview or plan.autostart or not version_supported(plan.true_version) or version != plan.true_version:
        raise InstallerError("Resume manifest is not a compatible live-install plan for this TrueNAS version")
    validate_vm_name(plan.vm_name)
    validate_name(plan.pool, "pool")
    if not 2 <= plan.vcpus <= 16 or plan.ram_mib < 2048 or plan.disk_gib < 32:
        raise InstallerError("Resume manifest contains resources below the supported minimums")
    if not plan.image_url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", plan.image_sha256) or plan.image_size < 0:
        raise InstallerError("Resume manifest contains invalid image metadata")
    if not 1 <= args.max_download_gib <= 10:
        raise InstallerError("Maximum download size must be between 1 and 10 GiB")
    parts = plan.zvol.split("/")
    if len(parts) != 3 or parts[0] != plan.pool or parts[1] != "haos-vms":
        raise InstallerError("Resume manifest has an unexpected zvol path")
    validate_name(parts[2], "zvol")
    parent = "/".join(parts[:-1])
    dev = f"/dev/zvol/{plan.zvol}"
    resources = data.get("resources")
    expected_zvol = {"type":"zvol","id":plan.zvol,"dev":dev}
    if not isinstance(resources, list) or resources.count(expected_zvol) != 1:
        raise InstallerError("Resume manifest does not positively identify this zvol as created by the installer")
    if sum(1 for resource in resources if isinstance(resource, dict) and resource.get("type") == "dataset") > 1:
        raise InstallerError("Resume manifest contains multiple dataset records")
    for resource in resources:
        if not isinstance(resource, dict):
            raise InstallerError("Resume manifest contains an invalid resource record")
        if resource.get("type") == "dataset" and resource.get("id") == parent:
            continue
        if resource == expected_zvol:
            continue
        raise InstallerError("Resume is allowed only while the manifest tracks the expected parent dataset and zvol, with no VM or devices")
    if args.zvol_name and args.zvol_name != parts[2]:
        raise InstallerError("--zvol-name does not match the saved resume plan")
    for arg_name, plan_name in (("pool","pool"),("nic","nic_attach"),("vm_name","vm_name"),("vcpus","vcpus"),("ram_mib","ram_mib"),("disk_gib","disk_gib")):
        requested = getattr(args,arg_name,None)
        if requested is not None and requested != getattr(plan,plan_name):
            raise InstallerError(f"--{arg_name.replace('_','-')} does not match the saved resume plan")
    if args.no_start and plan.start_vm:
        raise InstallerError("Saved plan starts the VM; omit --no-start or use a no-start manifest")
    if args.test_limits and (plan.vcpus > 2 or plan.ram_mib > 2048 or plan.disk_gib > 32):
        raise InstallerError("Saved plan exceeds --test-limits")
    if (args.image_url and args.image_url != plan.image_url) or (args.image_sha256 and args.image_sha256.lower() != plan.image_sha256):
        raise InstallerError("Image arguments do not match the saved resume plan")
    pools = client.call("pool.query")
    if not any(p.get("name") == plan.pool and p.get("healthy") and p.get("status") == "ONLINE" for p in pools):
        raise InstallerError(f"Pool {plan.pool!r} is not healthy and ONLINE")
    choices = client.call("vm.device.nic_attach_choices")
    nics = (choices.get("BRIDGE") or []) + (choices.get("MACVLAN") or [])
    if plan.nic_attach not in nics:
        raise InstallerError(f"Saved network attachment {plan.nic_attach!r} is no longer available")
    existing_vm_names = {manifest_vm_name, plan.vm_name}
    if any(v.get("name") in existing_vm_names for v in client.call("vm.query")):
        raise InstallerError("A VM with the saved or requested name already exists; refusing to overwrite or resume its storage")
    if any((device.get("attributes") or {}).get("path") == dev for device in client.call("vm.device.query")):
        raise InstallerError("The manifest zvol is attached to a VM device; refusing to overwrite it")
    zvols = client.call("pool.dataset.query", [[["id","=",plan.zvol]]])
    parent_ds = client.call("pool.dataset.query", [[["id","=",parent]]])
    if len(zvols) != 1 or zvols[0].get("type") != "VOLUME":
        raise InstallerError("Manifest-owned zvol is missing or has an unexpected type")
    if dataset_comment(zvols[0]) != f"Created by {APP} for {manifest_vm_name}":
        raise InstallerError("Manifest zvol does not have the expected installer ownership marker")
    size = zvols[0].get("volsize")
    if isinstance(size, dict): size = size.get("parsed")
    if int(size or 0) != plan.disk_gib * 1024**3:
        raise InstallerError("Manifest-owned zvol size differs from the saved plan")
    if len(parent_ds) != 1 or parent_ds[0].get("type") != "FILESYSTEM":
        raise InstallerError("Expected parent filesystem is missing or has an unexpected type")
    if any(r.get("type") == "dataset" and r.get("id") == parent for r in resources):
        if dataset_comment(parent_ds[0]) != f"Created by {APP} for HAOS VM disks":
            raise InstallerError("Manifest parent dataset does not have the expected installer ownership marker")
    asset = Path(urllib.parse.urlparse(plan.image_url).path).name
    if args.preview:
        plan = dataclasses.replace(plan, preview=True)
    return plan, {"info":info,"methods_checked":required,"haos_asset":asset,"resume":True}

def _schema_matches(value, schema, path="argument", root=None):
    """Check a payload against the JSON-schema subset emitted by middleware."""
    if not isinstance(schema, dict):
        return [f"{path}: middleware schema node is malformed"]
    if "$ref" in schema:
        ref = schema["$ref"]
        target = root
        try:
            if not isinstance(ref, str) or not ref.startswith("#/") or target is None:
                raise KeyError(ref)
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            return [f"{path}: unresolved middleware schema reference {ref!r}"]
        return _schema_matches(value, target, path, root)
    if "const" in schema and value != schema["const"]:
        return [f"{path}: expected {schema['const']!r}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: value {value!r} is not allowed by middleware enum"]
    if "oneOf" in schema:
        matches = [variant for variant in schema["oneOf"] if not _schema_matches(value, variant, path, root)]
        return [] if len(matches) == 1 else [f"{path}: must match exactly one middleware variant; matched {len(matches)}"]
    if "anyOf" in schema:
        return [] if any(not _schema_matches(value, variant, path, root) for variant in schema["anyOf"]) else [f"{path}: does not match any middleware alternative"]

    expected = schema.get("type")
    if expected:
        choices = expected if isinstance(expected, list) else [expected]
        def valid_type(kind):
            return ((kind == "object" and isinstance(value, dict)) or
                    (kind == "array" and isinstance(value, list)) or
                    (kind == "string" and isinstance(value, str)) or
                    (kind == "integer" and isinstance(value, int) and not isinstance(value, bool)) or
                    (kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)) or
                    (kind == "boolean" and isinstance(value, bool)) or
                    (kind == "null" and value is None))
        if not any(valid_type(kind) for kind in choices):
            return [f"{path}: expected type {' or '.join(choices)}, got {type(value).__name__}"]

    errors = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        required.update(k for k, spec in properties.items() if spec.get("_required_") is True)
        errors.extend(f"{path}.{key}: required field is missing" for key in sorted(required - value.keys()))
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}.{key}: field is not accepted" for key in sorted(value.keys() - properties.keys()))
        for key, item in value.items():
            if key in properties:
                errors.extend(_schema_matches(item, properties[key], f"{path}.{key}", root))
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                errors.extend(_schema_matches(item, items, f"{path}[{index}]", root))
        elif isinstance(items, list):
            for index, item_schema in enumerate(items[:len(value)]):
                errors.extend(_schema_matches(value[index], item_schema, f"{path}[{index}]", root))
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            errors.append(f"{path}: string length is outside middleware bounds")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            errors.append(f"{path}: string does not match middleware pattern")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            errors.append(f"{path}: number is outside middleware bounds")
    return errors


def validate_method_request(methods, method, params):
    metadata = methods.get(method)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("accepts"), list):
        raise InstallerError(f"TrueNAS runtime schema for {method} has no accepted-arguments definition")
    accepts = metadata["accepts"]
    if len(params) > len(accepts):
        raise InstallerError(f"Installer request for {method} has too many positional arguments")
    for index, schema in enumerate(accepts):
        if index >= len(params):
            if schema.get("_required_") is True:
                raise InstallerError(f"Installer request for {method} omits required argument {schema.get('_name_', index)!r}")
            continue
        errors = _schema_matches(params[index], schema, f"{method}[{schema.get('_name_', index)}]", metadata)
        if errors:
            raise InstallerError(f"TrueNAS runtime schema mismatch: {errors[0]}")


def call_checked_write(client, method, params):
    methods = getattr(client, "runtime_methods", None)
    if methods is None:
        methods = client.call("core.get_methods")
        if not isinstance(methods, dict):
            raise InstallerError("TrueNAS did not return runtime method schemas before write")
        client.runtime_methods = methods
    validate_method_request(methods, method, params)
    return client.call(method, params)


def validate_api_methods(methods):
    sample_vm = {
        "name": "SchemaCheck", "description": f"Home Assistant OS VM created by {APP} (BETA)",
        "vcpus": 2, "cores": 2, "threads": 1, "memory": 4096, "min_memory": None,
        "cpu_mode": "HOST-MODEL", "bootloader": "UEFI", "bootloader_ovmf": "OVMF_CODE.fd",
        "trusted_platform_module": False, "autostart": False, "ensure_display_device": True, "time": "UTC",
    }
    requests = {
        "pool.dataset.create": [
            [{"name": "tank/haos-vms/schemacheck", "type": "FILESYSTEM", "comments": f"Created by {APP} for HAOS VM disks"}],
            [{"name": "tank/haos-vms/schemacheck", "type": "VOLUME", "volsize": 64 * 1024**3, "comments": f"Created by {APP} for SchemaCheck"}],
        ],
        "vm.create": [[sample_vm]],
        "vm.device.create": [
            [{"vm": 1, "attributes": {"dtype": "DISK", "path": "/dev/zvol/tank/haos-vms/schemacheck", "type": "VIRTIO", "create_zvol": False, "iotype": "THREADS"}}],
            [{"vm": 1, "attributes": {"dtype": "NIC", "type": "VIRTIO", "nic_attach": "br-schema", "mac": "02:00:00:00:00:01"}}],
        ],
        "vm.start": [[1, {"overcommit": False}]],
    }
    for method, examples in requests.items():
        for params in examples:
            validate_method_request(methods, method, params)


def build_plan(client, args) -> tuple[Plan, dict]:
    info = client.call("system.info")
    version = client.call("system.version")
    methods = client.call("core.get_methods")
    required = ["vm.create", "vm.start", "vm.stop", "vm.query", "vm.device.create", "vm.device.delete", "vm.device.query", "vm.device.nic_attach_choices", "pool.dataset.create", "pool.dataset.query", "pool.query", "interface.query"]
    missing = [m for m in required if m not in methods]
    if missing:
        raise InstallerError("Required TrueNAS VM/storage API methods are missing: " + ", ".join(missing))
    validate_api_methods(methods)
    client.runtime_methods = methods
    if not version_supported(version):
        if not args.preview or not args.allow_unsupported_version:
            raise InstallerError(f"Unsupported TrueNAS version {version}. This BETA supports 25.10.x and 26.0.0-BETA.x. Unsupported versions may only be evaluated in read-only preview mode with --allow-unsupported-version; live installs remain blocked.")
    if args.resume_manifest:
        return build_resume_plan(client, args, info, version, required)
    pools = client.call("pool.query")
    healthy = [p for p in pools if p.get("healthy") and p.get("status") == "ONLINE"]
    if not healthy:
        raise InstallerError("No healthy ONLINE pools found")
    print("\nHealthy pools:")
    for p in healthy: print(" - " + summarize_pool(p))
    pool_name = validate_name(args.pool or select_storage_pool(healthy), "pool")
    pool = next((p for p in healthy if p["name"] == pool_name), None)
    if pool is None:
        raise InstallerError(f"Pool {pool_name!r} is not healthy and ONLINE; choose one of the listed pools")
    nic_choices = client.call("vm.device.nic_attach_choices")
    nics = (nic_choices.get("BRIDGE") or []) + (nic_choices.get("MACVLAN") or [])
    if not nics: raise InstallerError("No existing VM NIC attachment choices found. Refusing to alter host networking.")
    nic = args.nic or select_from("existing network attachment", nics, "br0" if "br0" in nics else ("br01" if "br01" in nics else nics[0]))
    if nic not in nics:
        raise InstallerError(f"Network attachment {nic!r} is not an existing TrueNAS VM attachment choice")
    vm_name = validate_vm_name(args.vm_name or ask("VM name", DEFAULTS["vm_name"]))
    vcpus = args.vcpus if args.vcpus is not None else ask_int("CPU cores", DEFAULTS["vcpus"], 2, 16)
    ram_mib = args.ram_mib if args.ram_mib is not None else ask_int("RAM MiB", DEFAULTS["ram_mib"], 2048)
    disk_gib = args.disk_gib if args.disk_gib is not None else select_disk_size(DEFAULTS["disk_gib"], args.test_limits)
    if args.test_limits:
        vcpus, ram_mib, disk_gib = min(vcpus,2), min(ram_mib,2048), min(disk_gib,32)
    if not 2 <= vcpus <= 16:
        raise InstallerError("CPU cores must be between 2 and 16")
    if ram_mib < 2048:
        raise InstallerError("RAM must be at least 2048 MiB")
    if disk_gib < 32:
        raise InstallerError("Disk size must be at least 32 GiB")
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
    suffix = " (PREVIEW - no changes will be made)" if plan.preview else (" (RESUME - manifest-owned partial install)" if meta.get("resume") else "")
    print("\nInstallation plan" + suffix)
    print(f"  TrueNAS version: {plan.true_version}")
    print(f"  HAOS image: {meta.get('haos_asset')} from official Home Assistant OS GitHub releases")
    print(f"  Image sha256: {plan.image_sha256}")
    print(f"  Pool: {plan.pool}")
    print(f"  {'Manifest-owned zvol' if meta.get('resume') else 'New zvol'}: {plan.zvol} ({plan.disk_gib} GiB)")
    print(f"  New VM: {plan.vm_name} ({plan.vcpus} vCPU, {plan.ram_mib} MiB RAM, autostart disabled)")
    print(f"  NIC attaches to existing: {plan.nic_attach}")
    print("  Firmware: UEFI, OVMF_CODE.fd, Secure Boot disabled")
    print(f"  Start after creation: {plan.start_vm}")

def ensure_private_cache_dir(cache_dir: Path) -> Path:
    cache_dir = Path(os.path.abspath(os.fspath(Path(cache_dir).expanduser())))
    try:
        cache_dir = ensure_trusted_directory_chain(cache_dir, "Image cache", create=True)
        info = cache_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise InstallerError("Image cache directory must be a real directory owned by the current user; choose a new --cache-dir rather than reusing another user's cache")
        if info.st_mode & 0o022:
            raise InstallerError("Image cache directory must not be group/world-writable; choose a new --cache-dir")
        os.chmod(cache_dir, 0o700)
    except InstallerError:
        raise
    except OSError as exc:
        raise InstallerError("Could not create a private image cache directory") from exc
    return cache_dir

def download_and_verify(url: str, sha256: str, cache_dir: Path, max_bytes: int = MAX_DOWNLOAD_BYTES_DEFAULT, expected_size: int|None = None) -> Path:
    if not url.startswith("https://"):
        raise InstallerError("HAOS image downloads must use HTTPS")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256 or ""):
        raise InstallerError("A valid 64-character SHA-256 is required for the HAOS image")
    if max_bytes <= 0:
        raise InstallerError("Download byte limit must be positive")
    if expected_size is not None and not 0 <= expected_size <= max_bytes:
        raise InstallerError(f"HAOS image asset size is outside the configured 0–{max_bytes} byte limit")
    cache_dir = ensure_private_cache_dir(cache_dir)
    dest = cache_dir / f"haos-{sha256.lower()}.qcow2.xz"
    try:
        cached_info = dest.lstat()
    except FileNotFoundError:
        cached_info = None
    if cached_info is not None:
        if not stat.S_ISREG(cached_info.st_mode) or cached_info.st_uid != os.geteuid():
            raise InstallerError("Refusing an unsafe or unowned HAOS cache entry")
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
        with open_https_url(req, timeout=120) as resp:
            if urllib.parse.urlparse(resp.geturl()).scheme.lower() != "https":
                raise InstallerError("HAOS image download did not remain on HTTPS")
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


def convert_qcow_to_zvol(qcow: Path, zvol_dev: str, manifest_path: Path):
    try:
        run_checked(["qemu-img", "convert", "-p", "-f", "qcow2", "-O", "raw", str(qcow), zvol_dev])
    except subprocess.CalledProcessError as exc:
        raise InstallerError(f"Disk conversion failed for {zvol_dev}. Any created resources remain recorded in {manifest_path}; fix the cause, then use --resume-manifest {manifest_path} instead of starting a fresh install") from exc

def run_checked(cmd: list[str], **kwargs):
    printable = " ".join(cmd[:3]) + (" ..." if len(cmd)>3 else "")
    print(f"Running: {printable}")
    subprocess.run(cmd, check=True, **kwargs)

def resolve_manifest_path(manifest_arg: str|None, vm_name: str) -> Path:
    if manifest_arg:
        return Path(manifest_arg).expanduser().resolve()
    return Path("/tmp") / f"haos-installer-{vm_name}-manifest.json"

def read_manifest_file(path: Path) -> dict:
    path = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    ensure_trusted_directory_chain(path.parent, "Task manifest")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(str(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise InstallerError("Resume manifest must be a regular file that is not group/world-writable")
        if info.st_uid not in {0, os.geteuid()}:
            raise InstallerError("Resume manifest must be owned by root or the current user")
        if info.st_size > 1024 * 1024:
            raise InstallerError("Resume manifest exceeds the 1 MiB safety limit")
        with os.fdopen(fd, "r") as source:
            fd = -1
            return json.load(source)
    finally:
        if fd >= 0:
            os.close(fd)

def create_manifest(path: Path, plan: Plan) -> dict:
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    try:
        ensure_trusted_directory_chain(path.parent, "Task manifest", create=True)
        manifest = {"tool": APP, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "resources": [], "plan": dataclasses.asdict(plan)}
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise InstallerError(f"Task manifest already exists at {path}; choose a new manifest path") from exc
    except PermissionError as exc:
        raise InstallerError(f"Cannot write task manifest to {path}; use --manifest with a writable location") from exc
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(manifest, indent=2))
        f.flush()
        os.fsync(f.fileno())
    return manifest

def update_manifest(path: Path, manifest: dict):
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    ensure_trusted_directory_chain(path.parent, "Task manifest")
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
    if os.geteuid() != 0:
        raise InstallerError("Live installation requires a root TrueNAS shell because qemu-img must write to /dev/zvol. No new resources were created by this run; rerun as root or use --resume-manifest after a partial run.")
    require_tools_for_install(False)
    manifest_path = Path(args.resume_manifest).expanduser() if args.resume_manifest else resolve_manifest_path(args.manifest, plan.vm_name)
    saved_vm_name = plan.vm_name
    if args.resume_manifest:
        try:
            manifest = read_manifest_file(manifest_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallerError(f"Cannot read resume manifest at {manifest_path}") from exc
        saved_plan = manifest.get("plan")
        current_plan = dataclasses.asdict(plan)
        if manifest.get("tool") != APP or not isinstance(saved_plan, dict):
            raise InstallerError("Resume manifest identity or plan is invalid")
        saved_vm_name = saved_plan.get("vm_name")
        if not isinstance(saved_vm_name, str) or not saved_vm_name or len(saved_vm_name) > 63:
            raise InstallerError("Resume manifest saved VM name is invalid")
        if saved_plan != current_plan:
            without_name = dict(current_plan)
            without_name["vm_name"] = saved_plan.get("vm_name")
            if not args.vm_name or saved_plan != without_name:
                raise InstallerError("Resume manifest plan changed since validation; refusing to continue")
            manifest["plan"] = current_plan
            update_manifest(manifest_path, manifest)
        parent = "/".join(plan.zvol.split("/")[:-1])
        expected_zvol = {"type":"zvol","id":plan.zvol,"dev":f"/dev/zvol/{plan.zvol}"}
        resources = manifest.get("resources")
        if not isinstance(resources, list) or resources.count(expected_zvol) != 1 or any(not isinstance(r, dict) or not (r == expected_zvol or (r.get("type") == "dataset" and r.get("id") == parent)) for r in resources):
            raise InstallerError("Resume manifest resources changed since validation; refusing to continue")
    else:
        manifest = create_manifest(manifest_path, plan)
    print(f"Task manifest: {manifest_path}")
    cache_dir = ensure_private_cache_dir(Path(args.cache_dir).expanduser())
    image = download_and_verify(
        plan.image_url,
        plan.image_sha256,
        cache_dir,
        max_bytes=min(MAX_DOWNLOAD_BYTES_DEFAULT, args.max_download_gib * 1024**3),
        expected_size=plan.image_size or None,
    )
    parent = "/".join(plan.zvol.split("/")[:-1])
    dev = f"/dev/zvol/{plan.zvol}"
    # Download, checksum, decompression, and QCOW2 validation all happen before
    # creating any TrueNAS datasets, zvols, VMs, or devices.
    with prepared_qcow_image(image, cache_dir, plan.disk_gib) as qcow:
        if args.resume_manifest:
            parent_matches = client.call("pool.dataset.query", [[["id","=",parent]]])
            zvol_matches = client.call("pool.dataset.query", [[["id","=",plan.zvol]]])
            if len(parent_matches) != 1 or parent_matches[0].get("type") != "FILESYSTEM":
                raise InstallerError("Resume parent dataset no longer matches the manifest")
            if any(r.get("type") == "dataset" and r.get("id") == parent for r in manifest.get("resources", [])):
                if dataset_comment(parent_matches[0]) != f"Created by {APP} for HAOS VM disks":
                    raise InstallerError("Resume parent dataset ownership marker changed")
            if len(zvol_matches) != 1 or zvol_matches[0].get("type") != "VOLUME":
                raise InstallerError("Resume zvol no longer matches the manifest")
            if dataset_comment(zvol_matches[0]) != f"Created by {APP} for {saved_vm_name}":
                raise InstallerError("Resume zvol ownership marker changed; refusing to overwrite it")
            volsize = zvol_matches[0].get("volsize")
            if isinstance(volsize, dict): volsize = volsize.get("parsed")
            if int(volsize or 0) != plan.disk_gib * 1024**3 or {"type":"zvol","id":plan.zvol,"dev":dev} not in manifest.get("resources",[]):
                raise InstallerError("Resume zvol size or manifest ownership check failed")
            if any(vm.get("name") in {saved_vm_name, plan.vm_name} for vm in client.call("vm.query")):
                raise InstallerError("A VM with the saved or requested name appeared after resume planning; refusing to continue")
            if any((device.get("attributes") or {}).get("path") == dev for device in client.call("vm.device.query")):
                raise InstallerError("The resume zvol became attached to a VM device; refusing to overwrite it")
        else:
            parent_matches = client.call("pool.dataset.query", [[["id","=",parent]]])
            if parent_matches:
                if any(d.get("type") != "FILESYSTEM" for d in parent_matches):
                    raise InstallerError(f"Parent dataset {parent!r} exists but is not a filesystem; refusing to use it")
            else:
                call_checked_write(client, "pool.dataset.create", [{"name": parent, "type":"FILESYSTEM", "comments": f"Created by {APP} for HAOS VM disks"}])
                manifest["resources"].append({"type":"dataset", "id":parent}); update_manifest(manifest_path, manifest)
            call_checked_write(client, "pool.dataset.create", [{"name": plan.zvol, "type":"VOLUME", "volsize": plan.disk_gib * 1024**3, "comments": f"Created by {APP} for {plan.vm_name}"}])
            manifest["resources"].append({"type":"zvol", "id":plan.zvol, "dev":dev}); update_manifest(manifest_path, manifest)
        if not Path(dev).exists():
            raise InstallerError(f"Created zvol device path is not visible: {dev}")
        if not os.access(dev, os.W_OK):
            raise InstallerError(f"No write permission for {dev}; run the installer from a root TrueNAS shell")
        # Destination is only a newly created or manifest-verified zvol.
        convert_qcow_to_zvol(qcow, dev, manifest_path)
    vm = call_checked_write(client, "vm.create", [{
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
    disk = call_checked_write(client, "vm.device.create", [{"vm": vm_id, "attributes":{"dtype":"DISK", "path":dev, "type":"VIRTIO", "create_zvol":False, "iotype":"THREADS"}}])
    manifest["resources"].append({"type":"device", "id":disk["id"], "kind":"DISK"}); update_manifest(manifest_path, manifest)
    nic = call_checked_write(client, "vm.device.create", [{"vm": vm_id, "attributes":{"dtype":"NIC", "type":"VIRTIO", "nic_attach":plan.nic_attach, "mac": random_mac()}}])
    manifest["resources"].append({"type":"device", "id":nic["id"], "kind":"NIC"}); update_manifest(manifest_path, manifest)
    if plan.start_vm:
        call_checked_write(client, "vm.start", [vm_id, {"overcommit": False}])
    print("\nCreated HAOS VM successfully.")
    if plan.start_vm:
        print("The VM is running. Open its console in TrueNAS to monitor the first boot.")
    else:
        print("The VM is powered off (--no-start). Start it in TrueNAS, then open its console to monitor the first boot.")
    print("Once booted, try http://homeassistant.local/; if needed use http://homeassistant.local:8123/ or the VM IP from DHCP.")
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
    ap.add_argument("--tls-ca-cert", default=os.getenv("TRUENAS_CA_CERT"), help="PEM CA certificate file for a self-signed/private TrueNAS WSS certificate")
    ap.add_argument("--insecure-tls", action="store_true", help="Explicitly disable TLS certificate and hostname verification for legacy self-signed setups; prefer --tls-ca-cert")
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
    ap.add_argument("--cache-dir", default=f"/tmp/haos-truenas-installer-cache-{os.geteuid()}")
    ap.add_argument("--manifest", help="Task manifest path (default: /tmp/haos-installer-<vm-name>-manifest.json)")
    ap.add_argument("--resume-manifest", help="Resume only an incomplete install whose exact resources are recorded in this manifest")
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
        client = MidcltClient() if local_midclt else TrueNASClient(args.truenas_url, args.username, args.password, args.api_key, tls_ca_cert=args.tls_ca_cert, insecure_tls=args.insecure_tls)
        try:
            plan, meta = build_plan(client, args)
            print_plan(plan, meta)
            if plan.preview:
                print("\nPreview complete. No resources were created or modified.")
                return 0
            if os.geteuid() != 0:
                raise InstallerError("Live installation requires a root TrueNAS shell because qemu-img must write to /dev/zvol. This check occurs before any download or resource creation.")
            if args.resume_manifest:
                confirm_text = ask("Type RESUME to continue this manifest-tracked partial install", "")
                if confirm_text != "RESUME":
                    raise InstallerError("Cancelled before any write operations")
            elif not args.yes:
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
