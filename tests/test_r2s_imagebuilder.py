import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('imagebuilder', ROOT / 'SCRIPTS/build_r2s_imagebuilder.py')
ib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ib)


class ImageBuilderTests(unittest.TestCase):
    def test_outline_switches_theme_and_disables_only_its_own_cron_entry(self):
        script = ROOT / 'profiles/r2s-overlay/etc/uci-defaults/30_luci-theme-outline'
        uci = """
uci() {
    shift
    case "$1:$2" in
        set:luci.main.mediaurlbase=*) media=${2#*=};;
        set:luci.themes.Outline=/luci-static/outline) registered=1;;
        delete:luci.themes.Aurora) aurora=;;
        delete:luci.themes.Vantage) vantage=;;
        delete:luci.themes.Bootstrap) bootstrap=;;
        commit:luci) printf '%s|%s|%s|%s|%s' "$media" "$registered" "$aurora" "$vantage" "$bootstrap";;
        get:outline.*) return 1;;
        set:outline.updates.auto=0) auto=0;;
        set:outline.*) return 0;;
        commit:outline) printf '|%s' "$auto";;
        *) return 99;;
    esac
}
media=$2
aurora=/luci-static/aurora
vantage=/luci-static/vantage
bootstrap=/luci-static/bootstrap
. "$1"
"""
        for previous in ('', '/luci-static/aurora', '/luci-static/bootstrap', '/luci-static/vantage'):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                for subdir in ('etc/config', 'etc/crontabs', 'www/luci-static'):
                    (root / subdir).mkdir(parents=True)
                cron = root / 'etc/crontabs/root'
                cron.write_text('0 4 * * 0 /usr/libexec/outline/job auto >/dev/null 2>&1\n'
                                '0 5 * * * /usr/bin/example\n')
                staged = root / 'defaults.sh'
                staged.write_text(script.read_text().replace('/etc/config/', str(root / 'etc/config') + '/')
                                  .replace('/etc/crontabs/', str(root / 'etc/crontabs') + '/')
                                  .replace('/www/', str(root / 'www') + '/'))
                # BSD sed and the router's BusyBox sed use different -i syntax.
                if sys.platform == 'darwin':
                    staged.write_text(staged.read_text().replace("sed -i ", "sed -i '' "))
                result = subprocess.check_output(['/bin/sh', '-c', uci, 'sh', str(staged), previous], text=True)
                self.assertEqual(result, '/luci-static/outline|1||||0')
                self.assertEqual(cron.read_text(), '0 5 * * * /usr/bin/example\n')

    def prepare_autocore_fixture(self, root):
        shutil.copytree(ROOT / 'profiles/r2s-status', root, dirs_exist_ok=True)
        script = root / 'etc/init.d/autocore'
        script.parent.mkdir(parents=True)
        shutil.copy2(ROOT / 'profiles/r2s-overlay/etc/init.d/autocore', script)
        for source, target in (
            ('etc/rpcd_luci', 'usr/share/rpcd/ucode/luci'),
            ('etc/ucode_sys', 'usr/share/ucode/luci/sys.uc'),
        ):
            dest = root / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / source, dest)
        return script

    def test_display_sources_are_pinned_and_tampering_is_rejected(self):
        ib.verify_status_sources()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            shutil.copytree(ROOT / 'profiles/r2s-status', root / 'profiles/r2s-status')
            target = root / 'profiles/r2s-status/etc/rpcd_luci'
            target.write_text(target.read_text() + '\n// unexpected update\n')
            with patch.object(ib, 'ROOT', root), self.assertRaisesRegex(RuntimeError, 'Pinned display source changed'):
                ib.verify_status_sources()

    def test_new_luci_rpc_interface_stops_the_build(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.prepare_autocore_fixture(root)
            (root / 'usr/share/rpcd/ucode/luci').write_text('\tnewRequiredMethod: {\n')
            with self.assertRaisesRegex(RuntimeError, 'LuCI interface missing'):
                ib.verify_autocore_display_only(root)

    def test_first_boot_display_installer_is_repeatable_and_preserves_network_config(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.prepare_autocore_fixture(root)
            network = root / 'etc/config/network'
            network.parent.mkdir()
            network.write_text('preserved network settings\n')
            for name in ('10_system.js', '29_ports.js'):
                target = root / 'www/luci-static/resources/view/status/include' / name
                target.parent.mkdir(parents=True, exist_ok=True)
            installer = (root / 'etc/uci-defaults/090-autocore').read_text()
            installer = installer.replace("'/", "'" + str(root) + "/")
            installer = installer.replace('/etc/init.d/rpcd restart', ':')
            for _ in range(2):
                subprocess.run(['/bin/sh', '-c', installer], check=True)
            self.assertEqual(network.read_text(), 'preserved network settings\n')
            self.assertEqual((root / 'usr/share/rpcd/ucode/luci').read_bytes(),
                             (ROOT / 'profiles/r2s-status/etc/rpcd_luci').read_bytes())

    def test_tuning_files_and_custom_kernel_modules_cannot_return(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ib.verify_no_custom_tuning(root)
            for path in ib.FORBIDDEN_TUNING_FILES:
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('# tuning\n')
                with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'Custom tuning file returned'):
                    ib.verify_no_custom_tuning(root)
                target.unlink()
            module = root / 'lib/modules/6.12.112/tcp_bbr3.ko'
            module.parent.mkdir(parents=True)
            module.write_bytes(b'custom module')
            with self.assertRaisesRegex(RuntimeError, 'Custom congestion module returned'):
                ib.verify_no_custom_tuning(root)

    def test_autocore_retains_display_with_no_network_commands(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = self.prepare_autocore_fixture(root)
            self.assertEqual(ib.verify_autocore_display_only(root)['mode'], 'display-only')
            # Tripwires fail if a future edit silently reintroduces tuning calls.
            guard = 'sysctl() { exit 91; }; uci() { exit 92; }; ethtool() { exit 93; }; ubus() { exit 94; }; '
            result = subprocess.run(['/bin/sh', '-c', guard + '. "$1"; start', 'sh', str(script)])
            self.assertEqual(result.returncode, 0)

    def test_autocore_network_tuning_cannot_return_in_image(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = self.prepare_autocore_fixture(root)
            script.write_text(script.read_text() + '\nsysctl -w net.ipv4.conf.br-lan.proxy_arp=1\n')
            with self.assertRaisesRegex(RuntimeError, 'network tuning returned'):
                ib.verify_autocore_display_only(root)

    def test_autocore_missing_display_extension_stops_build(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.prepare_autocore_fixture(root)
            (root / 'etc/rpcd_luci').unlink()
            with self.assertRaisesRegex(RuntimeError, 'display file differs'):
                ib.verify_autocore_display_only(root)

    def test_autocore_nonexecutable_service_stops_build(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = self.prepare_autocore_fixture(root)
            script.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'not executable'):
                ib.verify_autocore_display_only(root)

    def test_latest_includes_prereleases_excludes_drafts(self):
        data = [{'tag_name': 'v26.10.21', 'published_at': '2026-10-21', 'draft': False},
                {'tag_name': 'v26.10.29', 'published_at': '2026-10-29', 'draft': False, 'prerelease': True},
                {'tag_name': 'v26.11.01', 'published_at': '2026-11-01', 'draft': True}]
        self.assertEqual(ib.latest_release(data, r'v\d+\.\d+\.\d+', 'Xray')['tag_name'], 'v26.10.29')

    def test_passwall2_uses_latest_published_date(self):
        data = [{'tag_name': '26.10.20-1', 'published_at': '2026-10-20', 'draft': False},
                {'tag_name': '26.11.3-1', 'published_at': '2026-11-03', 'draft': False},
                {'tag_name': '26.11.4-1', 'published_at': None, 'draft': True}]
        self.assertEqual(ib.latest_release(data, r'\d+\.\d+\.\d+-\d+', 'Passwall2')['tag_name'], '26.11.3-1')

    def test_actual_apk_hard_dependency_is_required(self):
        packages = {k: '1-r1' for k in ib.POLICY['required']}
        packages['autocore-arm'] = ib.STATUS['version']
        packages['luci-theme-outline'] = ib.OUTLINE['version']
        packages['xray-core'] = '26.10.29-r1'
        with self.assertRaisesRegex(RuntimeError, 'new-hard-dependency'):
            ib.verify_packages(packages, ['new-hard-dependency'], '26.10.29', '1-r1')

    def test_all_protected_packages_and_all_forbidden_packages(self):
        base = {k: '1-r1' for k in ib.POLICY['required']}
        base['autocore-arm'] = ib.STATUS['version']
        base['luci-theme-outline'] = ib.OUTLINE['version']
        base['xray-core'] = '26.10.29-r1'
        ib.verify_packages(base, [], '26.10.29', '1-r1')
        for name in ib.POLICY['required']:
            with self.subTest(required=name), self.assertRaises(RuntimeError):
                ib.verify_packages({k: v for k, v in base.items() if k != name}, [], '26.10.29', '1-r1')
        for name in ib.POLICY['disabled']:
            with self.subTest(disabled=name), self.assertRaises(RuntimeError):
                ib.verify_packages({**base, name: '1-r1'}, [], '26.10.29', '1-r1')

    def test_stale_xray_cannot_pass(self):
        packages = {k: '1-r1' for k in ib.POLICY['required']}
        packages['autocore-arm'] = ib.STATUS['version']
        packages['luci-theme-outline'] = ib.OUTLINE['version']
        packages['xray-core'] = '26.10.21-r1'
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            ib.verify_packages(packages, [], '26.10.29', '1-r1')

    def test_feed_cannot_replace_selected_passwall2_or_add_themes(self):
        packages = {k: '1-r1' for k in ib.POLICY['required']}
        packages['autocore-arm'] = ib.STATUS['version']
        packages['luci-theme-outline'] = ib.OUTLINE['version']
        packages['xray-core'] = '26.10.29-r1'
        with self.assertRaisesRegex(RuntimeError, 'Passwall2 package is stale'):
            ib.verify_packages(packages, [], '26.10.29', '26.11.3-r1')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected LuCI themes'):
            ib.verify_packages({**packages, 'luci-theme-extra': '1-r1'}, [], '26.10.29', '1-r1')
        with self.assertRaisesRegex(RuntimeError, 'Outline package is not the pinned build'):
            ib.verify_packages({**packages, 'luci-theme-outline': '0-r1'}, [], '26.10.29', '1-r1')

    def test_manifest_parsing_ignores_build_output(self):
        self.assertEqual(ib.parse_manifest('make: downloading packages\nxray-core - 26.10.29-r1\n'), {'xray-core': '26.10.29-r1'})

    def test_missing_or_bad_release_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            asset = {'name': 'test.apk', 'size': 4, 'browser_download_url': 'https://example.org/test.apk'}
            with self.assertRaisesRegex(RuntimeError, 'digest'):
                ib.fetch_asset({'assets': [asset]}, 'test.apk', Path(d))
            asset['digest'] = 'sha256:' + '0' * 64
            with patch.object(ib, 'get', side_effect=lambda url, dest: Path(dest).write_bytes(b'bad!')):
                with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                    ib.fetch_asset({'assets': [asset]}, 'test.apk', Path(d))

    def test_partition_change_blocks_retained_config_upgrade(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'test.img'
            with path.open('wb') as f:
                f.truncate(962592768)
                mbr = bytearray(512);mbr[510:] = b'\x55\xaa'
                for index, start, count in ((0,65536,65536),(1,196608,1683456)):
                    mbr[446 + index * 16 + 4] = 131
                    struct.pack_into('<II',mbr,446 + index * 16 + 8,start,count)
                f.seek(0);f.write(mbr);f.seek(196608 * 512);f.write(b'hsqs')
            ib.check_layout(path, 'squashfs')
            with path.open('r+b') as f:
                f.seek(446 + 16 + 8);f.write(struct.pack('<I',131072))
            with self.assertRaisesRegex(RuntimeError, 'layout differs'):
                ib.check_layout(path, 'squashfs')

    def test_gzip_crc_and_openwrt_metadata_are_distinguished(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'image.gz';raw = Path(d) / 'image'
            compressor = zlib.compressobj(wbits=31)
            payload = compressor.compress(b'firmware' * 100) + compressor.flush()
            source.write_bytes(payload + b'fwtool metadata')
            self.assertEqual(ib.inflate(source, raw), b'fwtool metadata')
            self.assertEqual(raw.read_bytes(), b'firmware' * 100)
            damaged = bytearray(payload);damaged[-8] ^= 1;source.write_bytes(damaged)
            with self.assertRaises(zlib.error):ib.inflate(source, raw)
            source.write_bytes(payload[:-3])
            with self.assertRaisesRegex(RuntimeError, 'Truncated'):ib.inflate(source, raw)

    def test_only_reviewed_rng_exceptions_to_seed_exclusions(self):
        self.assertEqual(set(ib.POLICY['seed_exceptions']), {'urngd', 'urandom-seed'})
        self.assertTrue({'urngd', 'urandom-seed'} <= set(ib.POLICY['required']))
        self.assertFalse(set(ib.POLICY['required']) & set(ib.POLICY['disabled']))
        tokens = {l.strip() for l in (ROOT / 'profiles/packages.r2s').read_text().splitlines() if l and not l.startswith('#')}
        self.assertEqual(tokens, set(ib.POLICY['required']) | {'-' + x for x in ib.POLICY['disabled']})


if __name__ == '__main__':
    unittest.main()
