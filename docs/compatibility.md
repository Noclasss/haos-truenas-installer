# Compatibility and testing notes

## Version support policy

This BETA uses explicit TrueNAS version gates. Do not remove a gate merely to make the installer run on a local server.

A version may be added only after:

1. Reading the live API schema or official API docs for VM, VM device, dataset and network methods.
2. Verifying the required methods exist.
3. Confirming the VM creation payload fields for UEFI, OVMF, autostart, memory and CPU resources.
4. Confirming disk and NIC device schemas.
5. Running unit/mocked tests for duplicate-resource refusal and preview-mode no-write behavior.

## Checked versions

- `TrueNAS-26.0.0-BETA.2`
  - Read-only discovery performed against live API schema.
  - Required VM/storage/network methods were present.
  - Existing resources were inventoried privately and not published.
  - No live VM creation was performed during repository publication.

## Not yet proven

- Full live HAOS install on 25.10.x.
- Full live HAOS install on 26.0.0-BETA.x.
- HAOS onboarding endpoint response from a newly created VM.
- Other TrueNAS releases.

## Official image source

The installer selects the latest non-draft, non-prerelease Home Assistant OS release from:

- <https://github.com/home-assistant/operating-system/releases>

It selects the `haos_ova-<version>.qcow2.xz` asset and compares the downloaded image against the SHA-256 digest and expected size reported by GitHub's release asset API before converting the image. Because GitHub supplies both the artifact and digest, this detects corruption or mismatch but is not an independent signature.
