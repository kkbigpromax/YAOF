import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('imagebuilder', ROOT / 'SCRIPTS/build_r2s_imagebuilder.py')
ib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ib)


class ImageBuilderTests(unittest.TestCase):
    def test_latest_includes_prereleases_excludes_drafts(self):
        data = [{'tag_name': 'v26.10.21', 'published_at': '2026-10-21', 'draft': False},
                {'tag_name': 'v26.10.29', 'published_at': '2026-10-29', 'draft': False, 'prerelease': True},
                {'tag_name': 'v26.11.01', 'published_at': '2026-11-01', 'draft': True}]
        self.assertEqual(ib.latest_xray(data)['tag_name'], 'v26.10.29')

    def test_actual_apk_hard_dependency_is_required(self):
        packages = {k: '1-r1' for k in ib.POLICY['required']}
        packages['xray-core'] = '26.10.29-r1'
        with self.assertRaisesRegex(RuntimeError, 'new-hard-dependency'):
            ib.verify_packages(packages, ['new-hard-dependency'], '26.10.29')

    def test_all_protected_packages_and_all_forbidden_packages(self):
        base = {k: '1-r1' for k in ib.POLICY['required']}
        base['xray-core'] = '26.10.29-r1'
        ib.verify_packages(base, [], '26.10.29')
        for name in ib.POLICY['required']:
            with self.subTest(required=name), self.assertRaises(RuntimeError):
                ib.verify_packages({k: v for k, v in base.items() if k != name}, [], '26.10.29')
        for name in ib.POLICY['disabled']:
            with self.subTest(disabled=name), self.assertRaises(RuntimeError):
                ib.verify_packages({**base, name: '1-r1'}, [], '26.10.29')

    def test_stale_xray_cannot_pass(self):
        packages = {k: '1-r1' for k in ib.POLICY['required']}
        packages['xray-core'] = '26.10.21-r1'
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            ib.verify_packages(packages, [], '26.10.29')

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
