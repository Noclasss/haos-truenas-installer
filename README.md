# HAOS TrueNAS Installer — BETA

Guided, conservative installer for creating a **new** Home Assistant OS VM on TrueNAS SCALE.

This project is for community users who want a one-command, guided HAOS install on TrueNAS. It is deliberately labelled **BETA**: it refuses duplicate resources, has a read-only preview mode, and only claims compatibility for versions that have been checked against API schemas.

## One-command guided launcher

The copy/paste commands below are pinned to the published installer commit `7494d9711f1656fbf88bbe3f7a2801cac50046e7`. For reproducible use, keep the same commit SHA in both the URL and `HAOS_TRUENAS_INSTALLER_REF`. When a newer installer commit is published, update both occurrences to that full SHA.

Run a read-only preview first:

```sh
REF=7494d9711f1656fbf88bbe3f7a2801cac50046e7; curl -fsSL "https://raw.githubusercontent.com/Noclasss/haos-truenas-installer/$REF/install.sh" | HAOS_TRUENAS_INSTALLER_REF="$REF" sh -s -- --preview
```

When the preview looks right, paste this one-line guided install from a root shell, or keep `sudo` as shown:

```sh
REF=7494d9711f1656fbf88bbe3f7a2801cac50046e7; curl -fsSL "https://raw.githubusercontent.com/Noclasss/haos-truenas-installer/$REF/install.sh" | sudo env HAOS_TRUENAS_INSTALLER_REF="$REF" sh -s -- --no-start
```

The prompts let you choose the healthy storage pool, existing network attachment, VM name, CPU and RAM, and virtual disk size. The installer then displays the selected pool, zvol path and size, network, VM settings, and start behavior. Review that plan carefully; type `CREATE` only if it is correct. `--no-start` leaves the new VM powered off so you can start it yourself after creation. The pool menu chooses the TrueNAS storage pool for the VM disk; it does not select or repartition a physical drive.

Preview mode may be run from `main`, but live installs intentionally refuse an unpinned `main` launcher unless you set `HAOS_TRUENAS_ALLOW_UNPINNED=1`. You can also set `HAOS_TRUENAS_INSTALLER_REF=<commit>` when using the launcher from a branch copy of `install.sh`.

For local testing from a clone:

```sh
python3 haos_truenas_installer.py --preview
```

## What it does

- Checks TrueNAS version and required VM/storage/network API methods.
- Lists healthy pools and existing VM network attachment choices.
- Uses alphanumeric-only VM names as required by the TrueNAS VM API.
- Requires an explicit healthy storage-pool selection when `--pool` is not supplied; it displays each pool's free and total capacity and never chooses by emptiest-pool ranking.
- Offers virtual disk sizes of 32, 64, 128 or 256 GiB, plus a custom size; `--disk-gib` bypasses the menu.
- The selected disk size sets the HAOS VM's zvol capacity; it does not select or repartition a physical drive.
- Lets you choose an existing interface/bridge and VM resources.
- Defaults to:
  - 2 CPU cores
  - 4096 MiB RAM
  - 64 GiB disk
- Enforces minimum resources of 2 CPU cores, 2048 MiB RAM and 32 GiB disk.
- Downloads the latest stable official Home Assistant OS KVM/OVA QCOW2 image from the Home Assistant OS GitHub releases.
- Verifies the GitHub release asset SHA-256 digest and enforces a configured maximum download size before writing any TrueNAS resources.
- Stores image downloads in a private cache directory scoped to the effective user (`/tmp/haos-truenas-installer-cache-<uid>` by default); the cache and its parent path must be trusted, and the final directory must be owned by the installer process.
- Decompresses to a temporary seekable QCOW2 file and validates its format, backing-file status, and virtual size before creating datasets or a zvol; the temporary image is removed automatically.
- Preflights XZ expansion size before decompressing, requires the cache filesystem to have the expanded size plus a 1 GiB reserve free, and rejects expanded artifacts larger than the target VM disk plus 256 MiB.
- Creates only new resources:
  - parent dataset, if needed: `<pool>/haos-vms`
  - zvol: `<pool>/haos-vms/<vm-name>`
  - TrueNAS-managed VM
  - VM disk and NIC devices
- Uses UEFI with `OVMF_CODE.fd` so Secure Boot is disabled.
- Starts the VM unless `--no-start` is passed.
- Writes a task manifest listing every created resource before the first TrueNAS change. The default is `/tmp/haos-installer-<vm-name>-manifest.json`; pass `--manifest` for another trusted writable location.
- Uses restrictive permissions and atomic updates for the manifest, requires it to be owned by root/current user, and rejects symlinked or unsafe writable parent directories.

## What it refuses to do

- Overwrite an existing VM name.
- Overwrite an existing zvol/dataset path.
- Modify host networking, bridges, VLANs, IPs, routes, firewall rules or DNS.
- Attach existing disks, USB devices, PCI devices, Zigbee/Z-Wave sticks or production HA backups.
- Use memory overcommit when starting the VM.
- Install TrueNAS system packages.
- Remove anything not recorded in the task manifest.

## Compatibility

Current BETA gates:

- `TrueNAS-25.10.x`
- `TrueNAS-26.0.0-BETA.x`

The 26.0.0-BETA adapter was checked against a live read-only API schema from `TrueNAS-26.0.0-BETA.2`. Testing one release does **not** validate other TrueNAS releases.

The installer verifies at runtime that these methods exist:

- `vm.create`
- `vm.start`
- `vm.query`
- `vm.stop`
- `vm.device.create`
- `vm.device.delete`
- `vm.device.query`
- `vm.device.nic_attach_choices`
- `interface.query`
- `pool.dataset.create`
- `pool.dataset.query`
- `pool.query`

## Requirements

Run from a **root TrueNAS host shell** for the simplest live-install workflow. `qemu-img` must write directly to `/dev/zvol/...`; a non-root run is rejected before downloads or TrueNAS changes. In this mode the installer uses local `midclt` and does not need Python WebSocket dependencies or stored API credentials.

The launcher uses `/tmp` by default. If `TMPDIR` is set, it must be a real directory owned by root or the current user, with no unsafe group/world-writable ancestors; `/tmp`-style sticky directories are supported.

Remote/API mode is also possible if `websocket-client` 0.18.0 or newer is available in your controlled environment (or the `websockets` sync adapter is available), but a real install still needs access to write the verified converted image into the newly created `/dev/zvol/...` path.

Host tools needed for a real install:

- `python3`
- `curl` or `fetch`
- `midclt` for local host mode, or `websocket-client` 0.18.0+ / the `websockets` sync adapter for remote API mode
- `xz`
- `qemu-img`

Do not install packages into the TrueNAS host just to satisfy these. If a tool is missing, use preview mode and follow TrueNAS-supported methods for making required tools available.

## Authentication

When run on the TrueNAS host with `midclt`, no API token is needed.

Remote mode supports these environment variables:

```sh
export TRUENAS_URL=http://truenas.example.local
export TRUENAS_USERNAME=root
export TRUENAS_PASSWORD='...'
# or
export TRUENAS_API_KEY='...'
```

Do not put secrets in shell history. Prefer a root shell/session where credentials are not echoed.

For remote `wss://` connections, server-certificate and hostname verification are enabled by default. For a private/self-signed TrueNAS certificate, trust its issuing CA explicitly:

```sh
export TRUENAS_URL=https://truenas.example.local
export TRUENAS_CA_CERT=/path/to/truenas-ca.pem
# or pass --tls-ca-cert /path/to/truenas-ca.pem
```

The `websocket-client` adapter must be version 0.18.0 or newer to enforce hostname verification. `--insecure-tls` is an explicit compatibility override that disables both certificate and hostname checks; it prints a warning and should only be used temporarily on a trusted network. Remote `ws://` is plaintext and exposes credentials/API traffic; local `midclt` is unaffected.

The HAOS image’s SHA-256 and expected size are checked before use. GitHub supplies both the image and its digest through the same service, so this detects corruption/mismatch but is not an independent signature. Likewise, a commit-pinned installer archive over HTTPS still trusts GitHub to serve the source for that commit. The launcher extracts into a private temporary directory and rejects unsafe archive entries; pinning the commit remains the recommended supply-chain control.

## Preview mode

Preview mode performs API reads only and prints the install plan:

```sh
python3 haos_truenas_installer.py --preview
```

Preview mode must not create datasets, zvols, VMs, devices, downloads or manifests.

## Live-test limits

For disposable tests, use:

```sh
python3 haos_truenas_installer.py \
  --vm-name haosinstallertest$(date +%Y%m%d%H%M%S) \
  --disk-gib 32 \
  --ram-mib 2048 \
  --vcpus 2 \
  --test-limits \
  --no-start
```

Use a unique name. Keep the manifest. For long-term recovery, set `--manifest` to a writable persistent path or copy the default manifest out of `/tmp` after installation. Clean up only resources positively identified in that manifest.

## Recovery from interrupted installation

For a partial install that has only the manifest-owned parent dataset and zvol, run the pinned installer from a root TrueNAS shell with `--resume-manifest /path/to/manifest.json`. It validates the manifest, pool, network, zvol size, and absence of an existing VM, then requires typing `RESUME`. Resume never creates or overwrites the zvol; it writes only to that exact manifest-owned zvol and proceeds to create the VM/devices. Use `--vm-name NewAlphanumericName` on resume if the saved VM name is invalid for the TrueNAS API; this changes the pending VM name, not the zvol. Use `--preview --resume-manifest ...` first to check the saved plan without writes.

1. Open the manifest written by the installer, for example:

   ```sh
   cat /tmp/haos-installer-HAOS-manifest.json
   ```

2. Confirm each resource ID/path belongs to the interrupted test/install.
3. If a VM was created, stop only that VM from the TrueNAS UI or API.
4. Remove only the VM/devices/zvols listed in the manifest.
5. Do not use wildcards or prefix-only matching for cleanup.

## Accessing Home Assistant

If you used `--no-start`, start the VM in TrueNAS and open its console to monitor first boot. Once HAOS is booted, try:

- <http://homeassistant.local/>
- <http://homeassistant.local:8123/> if the first URL does not load
- the VM IP shown in your router/DHCP leases

The TrueNAS VM console is useful for watching startup and diagnosing boot issues. Do not restore a production backup into a disposable test VM unless that is your explicit, separate goal.
