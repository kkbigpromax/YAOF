#!/bin/bash
# Build-time only; run after package preparation, before make defconfig.
set -euo pipefail

# These unused recipes caused recursive Kconfig dependencies in the first build.
# Keep Passwall2/Xray and system recipes intact; seed controls optional packages.
test -f package/new/legacy/openwrt_helloworld/luci-app-passwall2/Makefile
test -f package/new/legacy/openwrt_helloworld/xray-core/Makefile
rm -rf package/new/legacy/openwrt_helloworld/luci-app-passwall \
  package/new/apps/homeproxy \
  package/new/legacy/openwrt_helloworld/luci-app-momo \
  package/new/legacy/openwrt_helloworld/momo \
  package/new/legacy/openwrt_helloworld/mihomo-alpha \
  package/new/legacy/openwrt_helloworld/mihomo-meta \
  package/new/apps/OpenWrt-mihomo \
  feeds/luci/applications/luci-app-weechat

for package_name in luci-app-passwall luci-app-homeproxy luci-app-momo momo mihomo-alpha \
  mihomo-meta luci-app-nikki nikki luci-app-weechat; do
  rm -f "package/feeds/luci/$package_name" "package/feeds/packages/$package_name"
done
