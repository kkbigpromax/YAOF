"""Fault-injection checks for the build gate, not firmware boot tests.

Fixtures: package manifest from successful build 39280782139; Passwall2 recipe
from QiuSimons/OpenWrt-Add at ec7092aa5437fe5608371f0723b54789f8fff0af.
Run: python3 -m unittest discover -s tests -v
"""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


class R2SVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.prepared = self.parent / "openwrt"
        seed = ROOT / "SEED/R2S/config.seed"
        (self.parent / "SEED/R2S").mkdir(parents=True)
        shutil.copyfile(seed, self.parent / "SEED/R2S/config.seed")
        disabled = set(re.findall(r"^# CONFIG_PACKAGE_(.+) is not set$", seed.read_text(), re.M))
        self.packages = dict(line.split(" - ", 1) for line in
                             (FIXTURES / "r2s-before-trim.manifest").read_text().splitlines())
        self.packages = {k: v for k, v in self.packages.items() if k not in disabled}
        self.packages["luci-app-passwall2"] = "26.10.20-r1"
        self.config = "\n".join("CONFIG_PACKAGE_" + p + "=y" for p in self.packages) + "\n"
        self.config += "CONFIG_TARGET_rockchip_armv8_DEVICE_friendlyarm_nanopi-r2s=y\n"
        self.config += "CONFIG_PACKAGE_dnsmasq_full_nftset=y\n"
        for option in ("Basic_Core_Xray", "Nftables_Transparent_Proxy"):
            self.config += "CONFIG_PACKAGE_luci-app-passwall2_" + option + "=y\n"
        recipe_dir = self.prepared / "package/new/legacy/openwrt_helloworld"
        for package, fixture in (("luci-app-passwall2", "passwall2.Makefile"),
                                 ("xray-core", "xray.Makefile")):
            (recipe_dir / package).mkdir(parents=True)
            shutil.copyfile(FIXTURES / fixture, recipe_dir / package / "Makefile")
        self.recipe = recipe_dir / "luci-app-passwall2/Makefile"
        self.target = self.prepared / "bin/targets/rockchip/armv8"
        self.target.mkdir(parents=True)
        self.manifest = self.target / "openwrt-rockchip-armv8-friendlyarm_nanopi-r2s.manifest"

    def run_gate(self, mode="config", config=None, packages=None):
        (self.prepared / ".config").write_text(self.config if config is None else config)
        packages = self.packages if packages is None else packages
        self.manifest.write_text("".join(f"{p} - {v}\n" for p, v in packages.items()))
        return subprocess.run([sys.executable, str(ROOT / "SCRIPTS/10_verify_r2s.py"), mode],
                              cwd=self.prepared, capture_output=True, text=True)

    def assert_rejected(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)

    def test_xray_only_selection_is_accepted(self):
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rtl8152_selection_does_not_require_generic_usbnet(self):
        config = self.config.replace("CONFIG_PACKAGE_kmod-usb-net=y\n", "")
        result = self.run_gate(config=config)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_removed_components_cannot_be_reselected(self):
        for package in ("sing-box", "haproxy", "shadowsocks-rust-sslocal",
                        "shadowsocksr-libev-ssr-redir", "simple-obfs-client", "v2ray-plugin",
                        "luci-app-upnp", "miniupnpd-nftables", "wireguard-tools",
                        "kmod-wireguard", "vmlinux-btf", "kmod-backlight-pwm", "mosdns",
                        "luci-app-flowcone", "flowcone", "luci-app-passwall",
                        "dns2socks", "microsocks", "ipt2socks", "nat6"):
            with self.subTest(package=package):
                self.assert_rejected(self.run_gate(config=self.config + f"CONFIG_PACKAGE_{package}=y\n"),
                                     "disabled packages selected: " + package)

    def test_default_optional_core_switch_cannot_regress(self):
        for option in ("Basic_Core_SingBox", "Basic_Core_All", "INCLUDE_Haproxy"):
            package = "luci-app-passwall2_" + option
            with self.subTest(option=option):
                self.assert_rejected(self.run_gate(config=self.config + f"CONFIG_PACKAGE_{package}=y\n"),
                                     "disabled packages selected: " + package)

    def test_passwall2_xray_and_dns_nftset_options_are_required(self):
        for symbol in ("luci-app-passwall2_Basic_Core_Xray",
                       "luci-app-passwall2_Nftables_Transparent_Proxy", "dnsmasq_full_nftset"):
            with self.subTest(symbol=symbol):
                self.assert_rejected(self.run_gate(config=self.config.replace(f"CONFIG_PACKAGE_{symbol}=y\n", "")),
                                     "missing CONFIG_PACKAGE_" + symbol)

    def test_withdrawn_bbr_patch_cannot_return(self):
        patch = self.prepared / "target/linux/generic/backport-6.12/010-0020-net-tcp_bbr-add-lfn-loss-tolerance.patch"
        patch.parent.mkdir(parents=True)
        patch.write_text("withdrawn patch\n")
        self.assert_rejected(self.run_gate(), "withdrawn BBR LFN patch is present")

    def test_boot_network_dns_and_ipv6_dependencies_are_protected(self):
        for package in ("base-files", "busybox", "procd", "fstools", "netifd", "dropbear",
                        "kmod-usb-net-rtl8152", "r8152-firmware", "kmod-mii", "kmod-libphy",
                        "kmod-crypto-sha256", "nftables-json", "kmod-nft-tproxy",
                        "chinadns-ng", "dnsmasq-full", "geoview", "v2ray-geosite",
                        "odhcp6c", "odhcpd-ipv6only", "kmod-nf-conntrack6", "kmod-tcp-bbr",
                        "luci-app-passwall2"):
            with self.subTest(package=package):
                config = self.config.replace(f"CONFIG_PACKAGE_{package}=y\n", "")
                self.assert_rejected(self.run_gate(config=config), "missing selections: " + package)

    def test_withdrawn_bcm_kernel_and_firewall_patches_cannot_return(self):
        for name in (
            "target/linux/generic/hack-6.12/982-add-bcm-fullconenat-support.patch",
            "package/network/config/firewall4/patches/999-01-firewall4-add-bcm-fullconenat-support.patch",
        ):
            with self.subTest(patch=name):
                patch = self.prepared / name
                patch.parent.mkdir(parents=True, exist_ok=True)
                patch.write_text("withdrawn patch\n")
                self.assert_rejected(self.run_gate(), "withdrawn BCM FullCone patch is present")
                patch.unlink()

    def test_prune_new_feed_layout_preserves_passwall2_xray_and_dns(self):
        unused = ("legacy/openwrt_helloworld/luci-app-passwall", "apps/homeproxy",
                  "legacy/openwrt_helloworld/momo", "apps/OpenWrt-mihomo")
        keep = ("legacy/openwrt_helloworld/luci-app-passwall2", "legacy/openwrt_helloworld/xray-core",
                "legacy/openwrt_helloworld/chinadns-ng", "apps/addition-trans-zh")
        for name in (*unused, *keep):
            path = self.prepared / "package/new" / name
            path.mkdir(parents=True, exist_ok=True)
            (path / "Makefile").touch()
        result = subprocess.run(["bash", str(ROOT / "SCRIPTS/09_prune_r2s.sh")],
                                cwd=self.prepared, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in unused:
            self.assertFalse((self.prepared / "package/new" / name).exists(), name)
        for name in keep:
            self.assertTrue((self.prepared / "package/new" / name / "Makefile").exists(), name)

    def test_xray_new_path_updates_to_latest_published_prerelease(self):
        # Exercise the real updater with local HTTP fixtures, never network or
        # a target binary. A newer draft must not win over a published prerelease.
        releases = self.parent / "releases.json"
        releases.write_text(json.dumps([
            {"tag_name": "v26.10.21", "published_at": "2026-10-21T00:00:00Z", "draft": False, "prerelease": False},
            {"tag_name": "v26.10.27", "published_at": "2026-10-27T00:00:00Z", "draft": False, "prerelease": True},
            {"tag_name": "v26.10.28", "published_at": "2026-10-28T00:00:00Z", "draft": True, "prerelease": True},
        ]))
        archive = self.parent / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            payload = b"public source fixture\n"
            member = tarfile.TarInfo("Xray-core-fixture/README")
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))
        bindir = self.parent / "bin"
        bindir.mkdir()
        curl = bindir / "curl"
        curl.write_text('''#!/bin/sh
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) shift; output="$1" ;;
        https://api.github.com/repos/XTLS/Xray-core/releases*) source="$TEST_RELEASES" ;;
        https://codeload.github.com/XTLS/Xray-core/tar.gz/v26.10.27) source="$TEST_ARCHIVE" ;;
    esac
    shift
done
[ -n "$source" ] && [ -n "$output" ] && cp "$source" "$output"
''')
        curl.chmod(0o755)
        env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
               "TEST_RELEASES": str(releases), "TEST_ARCHIVE": str(archive)}
        result = subprocess.run(["bash", str(ROOT / "SCRIPTS/09_update_xray.sh")],
                                cwd=self.prepared, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        recipe = (self.prepared / "package/new/legacy/openwrt_helloworld/xray-core/Makefile").read_text()
        self.assertIn("PKG_VERSION:=26.10.27\n", recipe)
        self.assertIn("PKG_HASH:=" + hashlib.sha256(archive.read_bytes()).hexdigest(), recipe)
        self.assertIn("Xray selected: v26.10.27", result.stdout)

    def test_nat_preparation_excludes_r2s_extensions_only(self):
        # Execute the NAT preparation section with real file copies and stubbed
        # downloads/patching, checking both R2S and the unchanged other-device policy.
        script = (ROOT / "SCRIPTS/02_prepare_package.sh").read_text()
        section = script.split("### Fullcone-NAT 部分 ###\n", 1)[1].split("### Other Kernel Hack 部分 ###", 1)[0]
        for relative in ("PATCH/pkgs/firewall", "PATCH/kernel/bcmfullcone"):
            shutil.copytree(ROOT / relative, self.parent / relative)
        kernel = self.prepared / "target/linux/generic/hack-6.12"
        kernel.mkdir(parents=True)
        (self.prepared / "feeds/luci").mkdir(parents=True)
        bindir = self.parent / "nat-test-bin"
        bindir.mkdir()
        for name, text in (("wget", "#!/bin/sh\nexit 0\n"),
                           ("patch", '#!/bin/sh\ncat >> "$TEST_PATCH_LOG"\n')):
            command = bindir / name
            command.write_text(text)
            command.chmod(0o755)
        log = self.parent / "patch-inputs.log"
        env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
               "KERNEL_VERSION": "6.12", "TEST_PATCH_LOG": str(log)}
        for target in ("R2S", "R4S"):
            result = subprocess.run(["bash", "-e", "-c", section], cwd=self.prepared,
                                    env={**env, "seed": target}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            firewall = self.prepared / "package/network/config/firewall4/patches"
            self.assertEqual((kernel / "982-add-bcm-fullconenat-support.patch").exists(), target != "R2S")
            self.assertEqual((firewall / "999-01-firewall4-add-bcm-fullconenat-support.patch").exists(), target != "R2S")
            for keep in ("001-fix-fw4-flow-offload.patch", "100-fw4-add-custom-nft-command-support.patch"):
                self.assertTrue((firewall / keep).exists(), keep)
            applied = log.read_text()
            self.assertEqual("Subject: [PATCH 3/7] luci-app-firewall: add ipv6 nat option" in applied, target != "R2S")
            self.assertEqual("Subject: [PATCH] luci-app-firewall: add BRCM full cone NAT option" in applied, target != "R2S")
            self.assertIn("Subject: [PATCH 4/7] luci-add-firewall: add custom nft rule support", applied)
            log.unlink()

    def test_prepared_recipe_new_hard_dependency_is_protected(self):
        self.recipe.write_text(self.recipe.read_text().replace("LUCI_DEPENDS:=", "LUCI_DEPENDS:=+new-helper "))
        self.assert_rejected(self.run_gate(), "missing selections: new-helper")

    def test_dependency_conflict_with_trim_policy_stops_build(self):
        self.recipe.write_text(self.recipe.read_text().replace("LUCI_DEPENDS:=", "LUCI_DEPENDS:=+sing-box "))
        self.assert_rejected(self.run_gate(), "required packages disabled: sing-box")

    def test_changed_dependency_syntax_needs_review(self):
        self.recipe.write_text(self.recipe.read_text().replace("LUCI_DEPENDS:=", "LUCI_DEPENDS:=+FEATURE:helper "))
        self.assert_rejected(self.run_gate(), "dependency syntax changed; review required")

    def test_trimmed_manifest_accepted_with_synthetic_gzip_payloads(self):
        # Valid gzip containers containing random test bytes, not bootable images.
        payload = gzip.compress(os.urandom(1024 * 1024 + 128))
        for filesystem in ("ext4", "squashfs"):
            (self.target / f"openwrt-rockchip-armv8-friendlyarm_nanopi-r2s-{filesystem}-sysupgrade.img.gz").write_bytes(payload)
        # Real r8152 dependencies must suffice when phone adapters no longer
        # pull the unrelated generic USB-network driver into the image.
        config = self.config.replace("CONFIG_PACKAGE_kmod-usb-net=y\n", "")
        packages = {k: v for k, v in self.packages.items() if k != "kmod-usb-net"}
        result = self.run_gate("image", config=config, packages=packages)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_removed_components_cannot_reappear_in_image(self):
        for package in ("sing-box", "haproxy", "miniupnpd-nftables", "kmod-wireguard", "vmlinux-btf",
                        "luci-app-flowcone", "flowcone", "luci-app-passwall",
                        "dns2socks", "microsocks", "ipt2socks", "nat6"):
            with self.subTest(package=package):
                self.assert_rejected(self.run_gate("image", packages={**self.packages, package: "test"}),
                                     "contains disabled packages: " + package)

    def test_image_missing_ethernet_dns_or_ipv6_stops_publication(self):
        for package in ("kmod-usb-net-rtl8152", "chinadns-ng", "kmod-nft-tproxy", "odhcp6c"):
            with self.subTest(package=package):
                packages = {k: v for k, v in self.packages.items() if k != package}
                self.assert_rejected(self.run_gate("image", packages=packages), "missing packages: " + package)

    def test_wrong_xray_image_version_stops_publication(self):
        self.assert_rejected(self.run_gate("image", packages={**self.packages, "xray-core": "0.0.0-r1"}),
                             "Xray in image does not match")


if __name__ == "__main__":
    unittest.main()
