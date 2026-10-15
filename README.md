# HAOS TrueNAS Installer — BETA

Guided, conservative installer for creating a **new** Home Assistant OS VM on TrueNAS SCALE.

This project is for community users who want a one-command, guided HAOS install on TrueNAS. It is deliberately labelled **BETA**: it refuses duplicate resources, has a read-only preview mode, and only claims compatibility for versions that have been checked against API schemas.

## One-command launcher

Pinned to a commit is safest. Replace `<commit>` with a published commit SHA from this repository. Preview mode may be run from `main`, but live installs intentionally refuse an unpinned `main` launcher unless you set `HAOS_TRUENAS_ALLOW_UNPINNED=1`.

```sh
curl -fsSL https://raw.githubusercontent.com/Noclasss/haos-truenas-installer/<commit>/install.sh | HAOS_TRUENAS_INSTALLER_REF=<commit> sh -s -- --preview
```

When the preview looks correct, run without `--preview`:

```sh
curl -fsSL https://raw.githubusercontent.com/Noclasss/haos-truenas-installer/<commit>/install.sh | HAOS_TRUENAS_INSTALLER_REF=<commit> sh
```

You can also set `HAOS_TRUENAS_INSTALLER_REF=<commit>` when using the launcher from a branch copy of `install.sh`.

For local testing from a clone:

```sh
python3 haos_truenas_installer.py --preview
```

## What it does

- Checks TrueNAS version and required VM/storage/network API methods.
- Lists healthy pools and existing VM network attachment choices.
- Lets you choose storage pool, existing interface/bridge, VM name and resources.
- Defaults to:
  - 2 CPU cores
  - 4096 MiB RAM
  - 64 GiB disk
- Downloads the latest stable official Home Assistant OS KVM/OVA QCOW2 image from the Home Assistant OS GitHub releases.
- Verifies the GitHub release asset SHA-256 digest and enforces a configured maximum download size before writing any TrueNAS resources.
- Decompresses to a temporary seekable QCOW2 file and validates its format, backing-file status, and virtual size before creating datasets or a zvol; the temporary image is removed automatically.
- Preflights XZ expansion size before decompressing, requires the cache filesystem to have the expanded size plus a 1 GiB reserve free, and rejects expanded artifacts larger than the target VM disk plus 256 MiB.
- Creates only new resources:
  - parent dataset, if needed: `<pool>/haos-vms`
  - zvol: `<pool>/haos-vms/<vm-name>`
  - TrueNAS-managed VM
  - VM disk and NIC devices
- Uses UEFI with `OVMF_CODE.fd` so Secure Boot is disabled.
- Starts the VM unless `--no-start` is passed.
- Writes a task manifest listing every created resource.

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

Run from the TrueNAS host shell for the simplest workflow. In that mode the installer uses the local `midclt` command and does not need Python WebSocket dependencies or stored API credentials.

Remote/API mode is also possible if the Python `websocket-client` package is available in your controlled environment, but a real install still needs access to write the verified converted image into the newly created `/dev/zvol/...` path.

Host tools needed for a real install:

- `python3`
- `curl` or `fetch`
- `midclt` for local host mode, or `websocket-client` for remote API mode
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
  --vm-name haos_installer_test_$(date +%Y%m%d_%H%M%S) \
  --disk-gib 32 \
  --ram-mib 2048 \
  --vcpus 2 \
  --test-limits \
  --no-start
```

Use a unique name. Keep the manifest. Clean up only resources positively identified in that manifest.

## Recovery from interrupted installation

1. Open the manifest written by the installer, for example:

   ```sh
   cat ./haos-installer-HAOS-manifest.json
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
