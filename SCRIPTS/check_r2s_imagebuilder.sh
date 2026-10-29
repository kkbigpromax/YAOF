#!/usr/bin/env bash
# Read-only compatibility audit on a Linux runner; no firmware publication.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/imagebuilder-check"
mkdir -p "$OUT"
cd "$OUT"
BASE="https://downloads.openwrt.org/releases/25.12.6/targets/rockchip/armv8"
curl -fSL --retry 3 "$BASE/sha256sums" -o sha256sums
curl -fSL --retry 3 "$BASE/profiles.json" -o profiles.json
IB="$(awk '/openwrt-imagebuilder-.*Linux-x86_64.tar.zst$/ {print $2}' sha256sums | sed 's/^\*//')"
[[ "$IB" =~ ^openwrt-imagebuilder-25\.12\.6-rockchip-armv8\.Linux-x86_64\.tar\.zst$ ]]
curl -fSL --retry 3 "$BASE/$IB" -o "$IB"
awk -v file="$IB" '$2 == file || $2 == "*" file' sha256sums | sha256sum -c -
tar --zstd -xf "$IB"
IB_DIR="$OUT/${IB%.tar.zst}"
curl -fSL --retry 3 https://api.github.com/repos/Openwrt-Passwall/openwrt-passwall2/releases/latest -o passwall2-release.json
python3 - <<'PY'
import json
from pathlib import Path
r=json.loads(Path('passwall2-release.json').read_text())
names=['packages_apk_aarch64_generic.zip']+[a['name'] for a in r['assets'] if a['name'].startswith(('luci-app-passwall2-','luci-i18n-passwall2-zh-cn-')) and a['name'].endswith('.apk')]
assets=[a for a in r['assets'] if a['name'] in names]
assert len(assets)==3, names
assert all(a.get('digest','').startswith('sha256:') for a in assets), 'missing release SHA256'
Path('downloads.tsv').write_text(''.join(a['browser_download_url']+'\t'+a['name']+'\t'+a['digest'][7:]+'\n' for a in assets))
print('Passwall2 release:',r['tag_name'])
PY
while IFS=$'\t' read -r url name digest; do
  curl -fSL --retry 3 "$url" -o "$name"
  printf '%s  %s\n' "$digest" "$name" | sha256sum -c -
done < downloads.tsv
unzip -l packages_apk_aarch64_generic.zip
mkdir -p unpacked
unzip -q packages_apk_aarch64_generic.zip -d unpacked
find unpacked -type f -name '*.apk' -exec cp -t "$IB_DIR/packages" {} +
cp ./luci-app-passwall2-*.apk ./luci-i18n-passwall2-zh-cn-*.apk "$IB_DIR/packages/"
APK="$IB_DIR/staging_dir/host/bin/apk"
"$APK" --version
"$APK" --help
"$APK" adbdump --help || true
"$APK" mkpkg --help || true
echo 'ImageBuilder repositories:'
cat "$IB_DIR/repositories"
echo 'ImageBuilder local packages:'
ls "$IB_DIR/packages/"
"$APK" adbdump "$IB_DIR"/packages/luci-app-passwall2-*.apk > passwall2-metadata.txt
cat passwall2-metadata.txt
echo 'Resolving official-kernel Passwall2 dependencies (required packages may not be skipped):'
make -C "$IB_DIR" manifest PROFILE=friendlyarm_nanopi-r2s \
  PACKAGES='luci-app-passwall2 luci-i18n-passwall2-zh-cn xray-core chinadns-ng geoview v2ray-geoip v2ray-geosite dnsmasq-full -dnsmasq luci-nginx luci-compat luci-proto-ipv6 kmod-nft-socket kmod-nft-tproxy kmod-nft-nat kmod-usb-net-rtl8152 r8152-firmware kmod-tcp-bbr' \
  2>&1 | tee manifest.log
