#!/usr/bin/env sh
set -eu
umask 077
REF=${UBUNTU_TRUENAS_INSTALLER_REF:-main}
case "$REF" in
  main)
    case " $* " in
      *" --preview "*) : ;;
      *)
        printf '%s\n' "ERROR: refusing an unpinned live install from main." >&2
        printf '%s\n' "Set UBUNTU_TRUENAS_INSTALLER_REF to a full commit SHA; --preview may use main." >&2
        exit 2
        ;;
    esac
    ;;
  *)
    case "$REF" in
      *[!0-9a-f]*)
        printf '%s\n' "ERROR: installer ref must be 'main' for preview or a lowercase 40-character commit SHA." >&2
        exit 2
        ;;
    esac
    if [ "${#REF}" -ne 40 ]; then
      printf '%s\n' "ERROR: installer ref must be 'main' for preview or a lowercase 40-character commit SHA." >&2
      exit 2
    fi
    ;;
esac
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' "ERROR: python3 is required." >&2
  exit 1
fi
TMPBASE=${TMPDIR:-/tmp}
case "$TMPBASE" in /*) ;; *) TMPBASE=/tmp ;; esac
TMPBASE=$(python3 - "$TMPBASE" <<'PY' || exit 1
import os
import pathlib
import stat
import sys
path = pathlib.Path(os.path.abspath(sys.argv[1]))
owners = {0, os.geteuid()}
try:
    for current in [*reversed(path.parents), path]:
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners:
            raise ValueError
        if info.st_mode & 0o022 and not (info.st_mode & stat.S_ISVTX and info.st_uid in owners):
            raise ValueError
except (OSError, ValueError):
    print("ERROR: TMPDIR must be a trusted real directory owned by root/current user.", file=sys.stderr)
    raise SystemExit(1)
print(path)
PY
) || {
  printf '%s\n' "ERROR: refusing an unsafe temporary-directory path." >&2
  exit 1
}
TMP=$(mktemp -d "$TMPBASE/ubuntu-server-truenas-installer.XXXXXX") || {
  printf '%s\n' "ERROR: could not create a private temporary directory." >&2
  exit 1
}
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT HUP INT TERM
SOURCE="$TMP/ubuntu_server_truenas_installer.py"
URL="https://raw.githubusercontent.com/Noclasss/haos-truenas-installer/$REF/ubuntu-server/ubuntu_server_truenas_installer.py"
printf '%s\n' "Downloading Ubuntu Server TrueNAS installer source for ref $REF..."
if command -v curl >/dev/null 2>&1; then
  curl --proto '=https' --proto-redir '=https' --max-filesize 16777216 -fsSL "$URL" -o "$SOURCE"
else
  python3 - "$URL" "$SOURCE" <<'PY'
import os
import sys
import urllib.parse
import urllib.request
url, target = sys.argv[1:]
limit = 16 * 1024 * 1024
class HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme.lower() != "https":
            raise ValueError("refusing an HTTPS-to-HTTP redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)
try:
    if urllib.parse.urlparse(url).scheme.lower() != "https":
        raise ValueError("source URL must use HTTPS")
    with urllib.request.build_opener(HTTPSOnlyRedirectHandler()).open(url, timeout=60) as response:
        if urllib.parse.urlparse(response.geturl()).scheme.lower() != "https":
            raise ValueError("source download left HTTPS")
        total = 0
        with open(target, "xb") as output:
            while True:
                chunk = response.read(min(1024 * 1024, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError("source exceeds the 16 MiB limit")
                output.write(chunk)
except Exception:
    try:
        os.unlink(target)
    except FileNotFoundError:
        pass
    print("ERROR: secure installer-source download failed.", file=sys.stderr)
    raise SystemExit(1)
PY
fi
if [ ! -s "$SOURCE" ]; then
  printf '%s\n' "ERROR: downloaded installer source is empty." >&2
  exit 1
fi
if ( : </dev/tty ) 2>/dev/null; then
  python3 "$SOURCE" "$@" </dev/tty
else
  python3 "$SOURCE" "$@"
fi
