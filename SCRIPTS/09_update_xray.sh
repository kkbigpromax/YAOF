#!/bin/bash
# Build-time only: select the newest published Xray release, including prereleases.
# Run in the prepared OpenWrt source directory; never run on the router.
set -euo pipefail

recipe=package/new/legacy/openwrt_helloworld/xray-core/Makefile
test -f "$recipe" || { echo "Xray package path changed; review before building." >&2; exit 1; }

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

curl --fail --location --silent --show-error --retry 3 \
  --connect-timeout 15 --max-time 120 \
  -H 'Accept: application/vnd.github+json' \
  'https://api.github.com/repos/XTLS/Xray-core/releases?per_page=100' \
  -o "$workdir/releases.json"

tag=$(jq -er '
  map(select(.draft == false
    and (.tag_name | test("^v[0-9]+\\.[0-9]+\\.[0-9]+$"))))
  | max_by(.published_at) | .tag_name
' "$workdir/releases.json")
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "No valid Xray release found." >&2; exit 1; }

curl --fail --location --silent --show-error --retry 3 \
  --connect-timeout 15 --max-time 300 \
  "https://codeload.github.com/XTLS/Xray-core/tar.gz/$tag" \
  -o "$workdir/xray.tar.gz"
tar -tzf "$workdir/xray.tar.gz" >/dev/null
source_hash=$(sha256sum "$workdir/xray.tar.gz" | cut -d ' ' -f 1)

python3 - "$recipe" "${tag#v}" "$source_hash" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text()
# Fail on a different recipe format, rather than publishing the old core silently.
for line in (
    'PKG_NAME:=xray-core',
    'PKG_SOURCE:=$(PKG_NAME)-$(PKG_VERSION).tar.gz',
    'PKG_SOURCE_URL:=https://codeload.github.com/XTLS/Xray-core/tar.gz/v$(PKG_VERSION)?',
):
    if line not in text.splitlines():
        raise SystemExit('Xray source recipe changed; review required: ' + line)
for key, value in (
    ('PKG_VERSION', sys.argv[2]),
    ('PKG_RELEASE', '1'),
    ('PKG_HASH', sys.argv[3]),
):
    text, count = re.subn(r'^' + key + r':=.*$', key + ':=' + value, text, flags=re.M)
    if count != 1:
        raise SystemExit('Expected exactly one ' + key + '; recipe unchanged.')
path.write_text(text)
PY

echo "Xray selected: $tag; source SHA256: $source_hash"
