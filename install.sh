#!/usr/bin/env sh
set -eu
REPO="${HAOS_TRUENAS_INSTALLER_REPO:-https://github.com/Noclasss/haos-truenas-installer}"
REF="${HAOS_TRUENAS_INSTALLER_REF:-main}"
TMP="${TMPDIR:-/tmp}/haos-truenas-installer.$$"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT INT TERM
mkdir -p "$TMP"
TARBALL="$TMP/source.tar.gz"
URL="$REPO/archive/$REF.tar.gz"
printf '%s\n' "Downloading HAOS TrueNAS installer from $REPO at $REF..."
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$URL" -o "$TARBALL"
elif command -v fetch >/dev/null 2>&1; then
  fetch -qo "$TARBALL" "$URL"
else
  printf '%s\n' "ERROR: curl or fetch is required." >&2
  exit 1
fi
tar -xzf "$TARBALL" -C "$TMP"
SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$SRC/haos_truenas_installer.py" "$@"
fi
printf '%s\n' "ERROR: python3 is required." >&2
exit 1
