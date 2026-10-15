# Ubuntu Server TrueNAS Installer — BETA

A separate guided installer for a **new Ubuntu Server 26.04 LTS VM** on TrueNAS SCALE. It does not modify the HAOS installer or support Ubuntu Desktop yet.

## Status and support boundary

- The installer is pinned to Canonical's Ubuntu Server 26.04 cloud image, build `release-20260927`, architecture `amd64`.
- Its SHA-256 is checked against `8800651811af9a85465ad1d552add729947bb16488dddb4a9b5305a3d97332b2`. This value was matched to Canonical's detached-signature-verified `SHA256SUMS` using the UEC Image Automatic Signing Key fingerprint `D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81`.
- Live creation is currently gated to the exact build `TrueNAS-26.0.0-BETA.2`. Other versions may only be inspected in preview with `--allow-unsupported-version`; live installation remains blocked.
- This feature has unit/mocked tests only. No Ubuntu VM was created or live-tested on TrueNAS as part of this code change. The HAOS installation success does not validate this Ubuntu installer.

## What it does

- Queries TrueNAS pools, VM names, network choices, method schemas and system memory before planning.
- Requires an explicit healthy pool and an existing NIC attachment; it never changes host networking.
- Requires a unique VM name and refuses existing datasets/zvols.
- Uses a new namespaced dataset and zvol, checks 20% pool free-space headroom, checks host memory limits, and leaves the VM powered off by default.
- Downloads only the pinned Ubuntu image over HTTPS, enforces its exact size and SHA-256, then checks that `qemu-img` sees a standalone QCOW2 with no backing file.
- Builds a unique NoCloud seed ISO for first boot. It creates an admin user with the supplied **public** SSH key, disables SSH password login and root SSH login, and never asks for or stores a private key or password.
- Requires the live TrueNAS method schema to advertise the VM, disk, NIC, CD-ROM, path and device-order fields before it proceeds.
- Writes a restrictive local task manifest before downloads or TrueNAS resource changes and records created resources as the run progresses.
- Requires typing `CREATE` unless `--yes` is explicitly passed. The VM is left powered off by default; `--start` is the opt-in to power it on after creation.

## Requirements

Run the installer from a **root TrueNAS host shell** for a live install. It uses local `midclt` and never installs packages automatically. Required host tools for live install:

- `python3`
- `midclt`
- `qemu-img`
- one of `xorriso`, `genisoimage`, or `mkisofs` to create the cloud-init seed ISO
- `curl` (or Python's standard HTTPS client fallback in the launcher)

If a required tool is missing, the installer stops before creating TrueNAS resources. Do not install packages into the TrueNAS base OS just to satisfy it; use preview mode or a TrueNAS-supported environment.

## Preview

The launcher is pinned to installer commit `c2191eeb843adbf1597690254678e057b911623b`. Run a read-only preview first:

```sh
REF=c2191eeb843adbf1597690254678e057b911623b; curl -fsSL "https://raw.githubusercontent.com/Noclasss/haos-truenas-installer/$REF/ubuntu-server/install.sh" | UBUNTU_TRUENAS_INSTALLER_REF="$REF" sh -s -- --preview
```

Preview queries TrueNAS and prints the plan; it does not download the Ubuntu image, create a manifest, or modify TrueNAS.

For a disposable live test, run from a root TrueNAS shell only after the preview is correct and the listed host tools are present:

```sh
REF=c2191eeb843adbf1597690254678e057b911623b; curl -fsSL "https://raw.githubusercontent.com/Noclasss/haos-truenas-installer/$REF/ubuntu-server/install.sh" | UBUNTU_TRUENAS_INSTALLER_REF="$REF" sh -s -- --test-limits
```

This still requires typing `CREATE`. It leaves the VM powered off unless `--start` is explicitly added. The interactive live flow prompts for pool, NIC, VM name, hostname, Linux username and resources, then asks you to paste one SSH **public** key line. The private key stays on your own computer. Alternatively, pass `--ssh-public-key-file` for a public-key file present on the TrueNAS host.

## First boot

If `--start` was not selected, start the VM in TrueNAS and open its console. Observe cloud-init first boot, then find the DHCP address in your router/TrueNAS network view and connect from your computer with the matching private key:

```sh
ssh ubuntuadmin@<vm-ip>
```

The installer does not determine the VM's DHCP address. The NoCloud ISO remains attached and contains only the public SSH key and first-boot metadata; retain it until you confirm the VM completed cloud-init.

## Testing limits and recovery

`--test-limits` clamps the plan to at most 2 vCPU, 2048 MiB RAM and 32 GiB disk. Use a unique VM name and keep the manifest. This first version does **not** implement automated resume or cleanup. If a live run fails after a TrueNAS change, do not rerun with a new manifest or delete by name. Keep the manifest and reconcile only the exact recorded resources after independent read-only verification.

## Image update procedure

The image URL and checksum are intentionally pinned to a dated release. To update them, select a stable Canonical cloud-image release, verify its `SHA256SUMS.gpg` with the published Ubuntu cloud-image signing key and verify the signer's fingerprint through an independent Canonical source, then update the immutable release URL, digest and exact byte count together. Run all tests and keep the feature BETA until a disposable live install and first boot have been confirmed.
