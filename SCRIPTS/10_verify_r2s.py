#!/usr/bin/env python3
"""Build-time checks, run from the prepared OpenWrt directory."""
import json
import os
from pathlib import Path
import re
import sys


REQUIRED = {
    "luci-app-passwall2", "xray-core", "chinadns-ng", "geoview", "v2ray-geoip", "v2ray-geosite", "dnsmasq-full",
    "firewall4", "luci-base", "luci-nginx", "luci-theme-aurora",
    # Protect boot, management, onboard USB Ethernet and future IPv6 support.
    "base-files", "busybox", "procd", "netifd", "fstools", "uci", "ubus",
    "ubox", "libc", "dropbear", "ca-bundle", "r8152-firmware",
    # r8152 uses the USB core directly, not the generic usbnet module.
    "kmod-usb-core", "kmod-usb-net-rtl8152", "kmod-mii", "kmod-libphy",
    "kmod-crypto-sha256",
    "nftables-json", "kmod-nft-core", "kmod-nft-socket", "kmod-nft-tproxy", "kmod-nft-nat",
    "kmod-nf-conntrack", "kmod-nf-conntrack6", "kmod-nf-nat",
    "kmod-nf-reject", "kmod-nf-reject6",
    "odhcp6c", "odhcpd-ipv6only", "luci-proto-ipv6", "ip-full", "kmod-tcp-bbr",
}


def passwall2_dependencies():
    """Read the actual prepared recipe so new hard dependencies cannot be missed."""
    recipe = Path("package/new/legacy/openwrt_helloworld/luci-app-passwall2/Makefile").read_text()
    require("PKG_NAME:=luci-app-passwall2" in recipe.splitlines(), "unexpected Passwall2 recipe")
    recipe = recipe.replace("\\\n", " ")
    match = re.search(r"^LUCI_DEPENDS\s*:?=\s*(.*)$", recipe, re.M)
    require(match, "cannot read Passwall2 LUCI_DEPENDS")
    dependencies = set()
    for token in match.group(1).split():
        require(re.fullmatch(r"\+[A-Za-z0-9_.+-]+", token),
                "Passwall2 dependency syntax changed; review required: " + token)
        dependencies.add(token[1:])
    require(dependencies, "empty Passwall2 LUCI_DEPENDS")
    return dependencies


def require(condition, message):
    if not condition:
        raise SystemExit("R2S verification failed: " + message)


def main():
    require(len(sys.argv) == 2 and sys.argv[1] in {"config", "image"},
            "usage: 10_verify_r2s.py config|image")
    config = Path(".config").read_text()
    selected = set(re.findall(r"^CONFIG_PACKAGE_(.+)=y$", config, re.M))
    seed = Path("../SEED/R2S/config.seed").read_text()
    disabled = set(re.findall(r"^# CONFIG_PACKAGE_(.+) is not set$", seed, re.M))
    required = REQUIRED | passwall2_dependencies()
    require(not required & disabled, "required packages disabled: " + ", ".join(sorted(required & disabled)))
    require(not required - selected, "missing selections: " + ", ".join(sorted(required - selected)))
    require(not disabled & selected, "disabled packages selected: " + ", ".join(sorted(disabled & selected)))
    for symbol in (
        "CONFIG_TARGET_rockchip_armv8_DEVICE_friendlyarm_nanopi-r2s",
        "CONFIG_PACKAGE_luci-app-passwall2_Basic_Core_Xray",
        "CONFIG_PACKAGE_luci-app-passwall2_Nftables_Transparent_Proxy",
        "CONFIG_PACKAGE_dnsmasq_full_nftset",
    ):
        require(symbol + "=y" in config.splitlines(), "missing " + symbol)

    withdrawn_patch = Path("target/linux/generic/backport-6.12/010-0020-net-tcp_bbr-add-lfn-loss-tolerance.patch")
    require(not withdrawn_patch.exists(), "withdrawn BBR LFN patch is present")
    for withdrawn in (
        "target/linux/generic/hack-6.12/982-add-bcm-fullconenat-support.patch",
        "package/network/config/firewall4/patches/999-01-firewall4-add-bcm-fullconenat-support.patch",
    ):
        require(not Path(withdrawn).exists(), "withdrawn BCM FullCone patch is present: " + withdrawn)
    recipe = Path("package/new/legacy/openwrt_helloworld/xray-core/Makefile").read_text()
    version = re.search(r"^PKG_VERSION:=(\d+\.\d+\.\d+)$", recipe, re.M)
    require(version, "unrecognized Xray package version")
    expected = version.group(1)
    print("R2S package selection verified; expected Xray " + expected)
    if sys.argv[1] == "config":
        return

    target = Path("bin/targets/rockchip/armv8")
    manifests = sorted(target.glob("*nanopi-r2s*.manifest"))
    require(manifests, "no R2S image manifest")
    reports = []
    for manifest in manifests:
        packages = {}
        for line in manifest.read_text().splitlines():
            name, separator, package_version = line.partition(" - ")
            if separator:
                packages[name] = package_version
        require(not required - packages.keys(), manifest.name + " missing packages: " +
                ", ".join(sorted(required - packages.keys())))
        require(not disabled & packages.keys(), manifest.name + " contains disabled packages: " +
                ", ".join(sorted(disabled & packages.keys())))
        require(re.fullmatch(re.escape(expected) + r"(?:[-+].*)?", packages["xray-core"]),
                "Xray in image does not match selected release: " + packages["xray-core"])
        reports.append({"manifest": manifest.name,
                        "packages": {name: packages[name] for name in sorted(required)}})
    images = []
    for filesystem in ("ext4", "squashfs"):
        matches = sorted(target.glob("*nanopi-r2s-" + filesystem + "-sysupgrade.img.gz"))
        require(len(matches) == 1, "expected one R2S " + filesystem + " sysupgrade image")
        image = matches[0]
        require(image.stat().st_size > 1024 * 1024, "unexpectedly small image: " + image.name)
        with image.open("rb") as stream:
            require(stream.read(2) == b"\x1f\x8b", "invalid gzip header: " + image.name)
        images.append(image.name)
    Path("r2s-verification.json").write_text(json.dumps({
        "commit": os.environ.get("GITHUB_SHA"), "expected_xray": expected,
        "manifests": reports, "images": images,
    }, ensure_ascii=False, indent=2) + "\n")
    print("R2S firmware manifests, required packages, Xray version and image files verified.")


if __name__ == "__main__":
    main()
