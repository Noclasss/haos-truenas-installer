#!/usr/bin/env sh
set -eu
umask 077
REPO="${HAOS_TRUENAS_INSTALLER_REPO:-https://github.com/Noclasss/haos-truenas-installer}"
REF="${HAOS_TRUENAS_INSTALLER_REF:-main}"
case "$REPO" in
  https://*) ;;
  *)
    printf '%s\n' "ERROR: installer repository must use HTTPS." >&2
    exit 2
    ;;
esac
if [ "$REF" = "main" ] && [ "${HAOS_TRUENAS_ALLOW_UNPINNED:-}" != "1" ]; then
  case " $* " in
    *" --preview "*) : ;;
    *)
      printf '%s\n' "ERROR: refusing an unpinned live install from main." >&2
      printf '%s\n' "Set HAOS_TRUENAS_INSTALLER_REF to a release tag or commit SHA, or run --preview first." >&2
      printf '%s\n' "For testing only, set HAOS_TRUENAS_ALLOW_UNPINNED=1." >&2
      exit 2
      ;;
  esac
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' "ERROR: python3 is required to safely validate and extract the installer archive." >&2
  exit 1
fi
TMPBASE=${TMPDIR:-/tmp}
case "$TMPBASE" in
  /*) ;;
  *) TMPBASE=/tmp ;;
esac
TMP=$(mktemp -d "$TMPBASE/haos-truenas-installer.XXXXXX") || {
  printf '%s\n' "ERROR: could not create a private temporary directory." >&2
  exit 1
}
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT HUP INT TERM
TARBALL="$TMP/source.tar.gz"
EXTRACT_DIR="$TMP/extracted"
mkdir -m 700 "$EXTRACT_DIR"
URL="${REPO%/}/archive/$REF.tar.gz"
printf '%s\n' "Downloading HAOS TrueNAS installer archive for ref $REF..."
if command -v curl >/dev/null 2>&1; then
  curl --proto '=https' --proto-redir '=https' --max-filesize 134217728 -fsSL "$URL" -o "$TARBALL"
elif command -v fetch >/dev/null 2>&1; then
  python3 - "$URL" "$TARBALL" <<'PY' || exit 1
import os
import sys
import urllib.parse
import urllib.request

url, destination = sys.argv[1:]
limit = 128 * 1024 * 1024
class HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme.lower() != "https":
            raise ValueError("refusing an HTTPS-to-HTTP redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)
try:
    if urllib.parse.urlparse(url).scheme.lower() != "https":
        raise ValueError("archive URL must use HTTPS")
    with urllib.request.build_opener(HTTPSOnlyRedirectHandler()).open(url, timeout=120) as response:
        if urllib.parse.urlparse(response.geturl()).scheme.lower() != "https":
            raise ValueError("archive download did not remain on HTTPS")
        with open(destination, "xb") as output:
            total = 0
            while True:
                chunk = response.read(min(1024 * 1024, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError("archive exceeds the 128 MiB safety limit")
                output.write(chunk)
except Exception as exc:
    try:
        os.unlink(destination)
    except FileNotFoundError:
        pass
    print("ERROR: secure archive download failed", file=sys.stderr)
    raise SystemExit(1)
PY
else
  printf '%s\n' "ERROR: curl or fetch is required." >&2
  exit 1
fi
ARCHIVE_BYTES=$(wc -c < "$TARBALL" | tr -d '[:space:]')
case "$ARCHIVE_BYTES" in
  ''|*[!0-9]*)
    printf '%s\n' "ERROR: could not determine installer archive size." >&2
    exit 1
    ;;
esac
if [ "$ARCHIVE_BYTES" -gt 134217728 ]; then
  printf '%s\n' "ERROR: installer archive exceeds the 128 MiB safety limit." >&2
  exit 1
fi
ROOT=$(python3 - "$TARBALL" "$EXTRACT_DIR" <<'PY'
import pathlib
import sys
import tarfile

archive_path, extract_path = sys.argv[1:]
extract_root = pathlib.Path(extract_path)
max_members = 10000
max_expanded_bytes = 128 * 1024 * 1024
try:
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = []
        roots = set()
        seen = set()
        total = 0
        for member in archive:
            if len(members) >= max_members:
                raise ValueError("archive contains too many entries")
            name = member.name
            if not name or "\\" in name or "\x00" in name or name.startswith("/"):
                raise ValueError("archive contains an unsafe path")
            raw_parts = name.split("/")
            if raw_parts[-1] == "":
                raw_parts.pop()
            if not raw_parts or any(part in ("", ".", "..") for part in raw_parts):
                raise ValueError("archive contains an unsafe path")
            roots.add(raw_parts[0])
            normalized = "/".join(raw_parts)
            if normalized in seen:
                raise ValueError("archive contains duplicate paths")
            seen.add(normalized)
            if not (member.isdir() or member.isfile()):
                raise ValueError("archive contains a link or unsupported entry type")
            if member.size < 0:
                raise ValueError("archive contains a negative file size")
            total += member.size
            if total > max_expanded_bytes:
                raise ValueError("archive exceeds the expanded-size safety limit")
            members.append((member, raw_parts))
        if len(roots) != 1:
            raise ValueError("archive must contain exactly one top-level directory")
        root = next(iter(roots))
        if not root or root in (".", "..") or ":" in root:
            raise ValueError("archive contains an invalid top-level directory")
        files = {"/".join(parts) for member, parts in members if member.isfile()}
        if f"{root}/haos_truenas_installer.py" not in files or f"{root}/install.sh" not in files:
            raise ValueError("archive is missing the expected installer files")
        for member, parts in members:
            target = extract_root.joinpath(*parts)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("archive file could not be read")
            with source, target.open("xb") as output:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("archive file ended before its declared size")
                    output.write(chunk)
                    remaining -= len(chunk)
        print(root)
except (OSError, tarfile.TarError, ValueError) as exc:
    print(f"ERROR: unsafe or invalid installer archive: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
) || exit 1
SRC="$EXTRACT_DIR/$ROOT"
if ( : </dev/tty ) 2>/dev/null; then
  python3 "$SRC/haos_truenas_installer.py" "$@" </dev/tty
else
  python3 "$SRC/haos_truenas_installer.py" "$@"
fi