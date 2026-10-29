#!/usr/bin/env python3
"""Assemble the slim R2S with an unmodified official kernel; never run on a router."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tarfile
from urllib.request import Request, urlopen
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / 'profiles/r2s-policy.json').read_text())
API = 'https://api.github.com/repos/'


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def get(url, dest=None):
    req = Request(url, headers={'User-Agent': 'YAOF-R2S-ImageBuilder', 'Accept': 'application/vnd.github+json' if url.startswith(API) else '*/*'})
    with urlopen(req, timeout=120) as response:
        if dest is None:
            return response.read()
        with Path(dest).open('wb') as stream:
            shutil.copyfileobj(response, stream, 4 << 20)


def run(args, cwd=None, capture=False):
    result = subprocess.run([str(x) for x in args], cwd=cwd, check=True,
                            text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def latest_xray(releases):
    candidates = [r for r in releases if not r['draft'] and r.get('published_at') and re.fullmatch(r'v\d+\.\d+\.\d+', r['tag_name'])]
    require(candidates, 'No published Xray release')
    return max(candidates, key=lambda r: r['published_at'])


def fetch_asset(release, name, directory):
    matches = [a for a in release['assets'] if a['name'] == name]
    require(len(matches) == 1, 'Missing or ambiguous release asset: ' + name)
    asset = matches[0]
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', asset.get('digest') or ''), 'Missing GitHub asset digest: ' + name)
    dest = directory / name
    get(asset['browser_download_url'], dest)
    require(dest.stat().st_size == asset['size'] and sha(dest) == asset['digest'][7:], 'Asset hash/size mismatch: ' + name)
    print('Verified release asset:', name, asset['digest'], flush=True)
    return dest


def package_info(apk, path):
    data = json.loads(run([apk, 'adbdump', '--format', 'json', path], capture=True))
    if isinstance(data, list):
        require(len(data) == 1, 'Unexpected ADB object count')
        data = data[0]
    require(isinstance(data.get('info'), dict), 'Unexpected APK metadata structure')
    return data['info']


def parse_manifest(text):
    return dict(re.findall(r'^([A-Za-z0-9_.+-]+) - ([^\s]+)$', text, re.M))


def verify_packages(packages, hard_deps, xray_version):
    required = set(POLICY['required']) | set(hard_deps)
    missing = required - packages.keys()
    banned = set(POLICY['disabled']) & packages.keys()
    require(not missing, 'Missing protected packages: ' + ', '.join(sorted(missing)))
    require(not banned, 'Disabled packages returned: ' + ', '.join(sorted(banned)))
    require(packages['xray-core'].startswith(xray_version + '-'), 'Xray package is stale')
    return sorted(required)


def inflate(image, raw):
    """Check gzip CRC, retaining legitimate OpenWrt fwtool metadata outside gzip."""
    decoder = zlib.decompressobj(31)
    tail = b''
    with image.open('rb') as src, raw.open('wb') as dst:
        for chunk in iter(lambda: src.read(1 << 20), b''):
            if decoder.eof:
                tail += chunk
            else:
                dst.write(decoder.decompress(chunk))
                if decoder.eof:
                    tail += decoder.unused_data
        require(decoder.eof, 'Truncated gzip stream: ' + image.name)
    return tail


def check_layout(raw, filesystem):
    with raw.open('rb') as stream:
        mbr = stream.read(512)
        require(mbr[510:] == b'\x55\xaa', 'Invalid MBR')
        parts = []
        for i in range(4):
            entry = mbr[446 + i * 16:462 + i * 16]
            start, count = struct.unpack_from('<II', entry, 8)
            if count:
                require((start + count) * 512 <= raw.stat().st_size, 'Partition exceeds image')
                parts.append({'start': start, 'sectors': count, 'type': entry[4]})
        require(parts == [{'start': 65536, 'sectors': 65536, 'type': 131}, {'start': 196608, 'sectors': 1683456, 'type': 131}],
                'Partition layout differs from retained-config baseline; review required: ' + str(parts))
        stream.seek(parts[1]['start'] * 512 + (1080 if filesystem == 'ext4' else 0))
        require(stream.read(2 if filesystem == 'ext4' else 4) == (b'\x53\xef' if filesystem == 'ext4' else b'hsqs'), 'Invalid root filesystem')
    return parts


def configure_layout(ib):
    config = ib / '.config'
    text = config.read_text()
    for key, value in [('CONFIG_TARGET_KERNEL_PARTSIZE', 32), ('CONFIG_TARGET_ROOTFS_PARTSIZE', 822)]:
        text, count = re.subn(r'^' + key + r'=\d+$', key + '=' + str(value), text, flags=re.M)
        require(count == 1, 'ImageBuilder layout setting changed: ' + key)
    config.write_text(text)


def assemble(build):
    require(sys.platform.startswith('linux'), 'ImageBuilder requires a Linux runner; do not execute target binaries on Mac')
    work = ROOT / 'imagebuilder-r2s'
    work.mkdir(exist_ok=True)
    evidence = work / 'evidence'
    evidence.mkdir(exist_ok=True)
    downloads = work / 'downloads'
    downloads.mkdir(exist_ok=True)
    version = POLICY['openwrt']
    base = f'https://downloads.openwrt.org/releases/{version}/targets/rockchip/armv8'
    sums = get(base + '/sha256sums').decode()
    name = f'openwrt-imagebuilder-{version}-rockchip-armv8.Linux-x86_64.tar.zst'
    hashes = {line.split()[1].lstrip('*'): line.split()[0] for line in sums.splitlines() if len(line.split()) == 2}
    require(name in hashes, 'Official ImageBuilder checksum missing')
    archive = downloads / name
    get(base + '/' + name, archive)
    require(sha(archive) == hashes[name], 'Official ImageBuilder checksum mismatch')
    (evidence / 'official-sha256sums').write_text(sums)
    profiles = json.loads(get(base + '/profiles.json'))
    require(POLICY['profile'] in profiles['profiles'], 'Official R2S profile missing')
    (evidence / 'profiles.json').write_text(json.dumps(profiles, indent=2))
    ib = work / name.removesuffix('.tar.zst')
    if ib.exists():
        shutil.rmtree(ib)
    run(['tar', '--zstd', '-xf', archive, '-C', work])
    apk = ib / 'staging_dir/host/bin/apk'
    require(apk.is_file(), 'Host apk tool missing')
    configure_layout(ib)
    provenance = {'official_imagebuilder': {'url': base + '/' + name, 'sha256': hashes[name]},
                  'kernel_policy': 'Official precompiled kernel and modules; no YAOF kernel/firewall patches applied',
                  'commit': os.environ.get('GITHUB_SHA'), 'passwall2_release': POLICY['passwall2_release']}
    # All imported user-space packages are authenticated by GitHub Release digests.
    release = json.loads(get(API + 'Openwrt-Passwall/openwrt-passwall2/releases/tags/' + POLICY['passwall2_release']))
    bundle = fetch_asset(release, 'packages_apk_aarch64_generic.zip', downloads)
    wanted = {'chinadns-ng', 'geoview', 'tcping', 'v2ray-geoip', 'v2ray-geosite'}
    with zipfile.ZipFile(bundle) as z:
        require(z.testzip() is None, 'Corrupt Passwall2 APK bundle')
        for entry in z.infolist():
            n = Path(entry.filename).name
            match = re.fullmatch(r'(.+)-\d[^/]*\.apk', n)
            if match and match.group(1) in wanted:
                (ib / 'packages' / n).write_bytes(z.read(entry))
    app_names = [a['name'] for a in release['assets'] if a['name'].endswith('.apk') and a['name'].startswith(('luci-app-passwall2-', 'luci-i18n-passwall2-zh-cn-'))]
    require(len(app_names) == 2, 'Passwall2 UI/i18n assets changed')
    for asset in app_names:
        path = fetch_asset(release, asset, downloads)
        shutil.copy2(path, ib / 'packages' / path.name)
    app = next((ib / 'packages').glob('luci-app-passwall2-*.apk'))
    info = package_info(apk, app)
    require(info['name'] == 'luci-app-passwall2', 'Unexpected proxy package')
    hard_deps = []
    for dep in info['depends']:
        require(isinstance(dep, str) and re.fullmatch(r'[A-Za-z0-9_.+-]+', dep), 'Passwall2 dependency syntax changed: ' + str(dep))
        hard_deps.append(dep)
    provenance['passwall2_metadata'] = info
    # Generate a local build key; never upload its private half or put it in the image.
    private = work / 'build-key.pem'
    run(['openssl', 'genrsa', '-out', private, '2048'])
    private.chmod(0o600)
    public = ib / 'keys/r2s-build.pub'
    run(['openssl', 'rsa', '-in', private, '-pubout', '-out', public])
    for package in (ib / 'packages').glob('*.apk'):
        meta = package_info(apk, package)
        if meta['name'] in wanted | {'luci-app-passwall2', 'luci-i18n-passwall2-zh-cn'}:
            # Publisher APKs were already authenticated by the Release SHA256.
            # Only this signature conversion accepts their unavailable original key;
            # the resulting signature and ImageBuilder package checks remain strict.
            run([apk, 'adbsign', '--allow-untrusted', '--reset-signatures', '--sign-key', private, package])
            require(package_info(apk, package) == meta, 'APK metadata changed while signing')
            run([apk, '--keys-dir', ib / 'keys', 'verify', package])
    # Package the newest upstream ARM64 binary without rebuilding or patching the kernel.
    xrelease = latest_xray(json.loads(get(API + 'XTLS/Xray-core/releases?per_page=100')))
    xv = xrelease['tag_name'][1:]
    xb = fetch_asset(xrelease, 'Xray-linux-arm64-v8a.zip', downloads)
    xr = work / 'xray-root/usr/bin'
    xr.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(xb) as z:
        require(z.testzip() is None and 'xray' in z.namelist(), 'Invalid Xray archive')
        (xr / 'xray').write_bytes(z.read('xray'))
    binary = xr / 'xray'
    binary.chmod(0o755)
    run(['sudo', 'chown', '-R', '0:0', work / 'xray-root'])
    header = binary.read_bytes()[:20]
    require(header[:4] == b'\x7fELF' and struct.unpack_from('<H', header, 18)[0] == 183, 'Xray is not ARM64 ELF')
    output = run(['timeout', '30', 'qemu-aarch64-static', binary, 'version'], capture=True)
    require(re.search(r'\bXray ' + re.escape(xv) + r'\b', output), 'Downloaded Xray version mismatch: ' + output)
    print('Xray selected:', xrelease['tag_name'], 'prerelease:', xrelease['prerelease'], flush=True)
    xapk = ib / 'packages' / f'xray-core-{xv}-r1.apk'
    run([apk, 'mkpkg', '--sign-key', private, '--files', work / 'xray-root', '--output', xapk,
         '--info', 'name:xray-core', '--info', 'version:' + xv + '-r1', '--info', 'arch:aarch64_generic',
         '--info', 'description:Official Xray ARM64 release for Passwall2', '--info', 'license:MPL-2.0',
         '--info', 'url:https://github.com/XTLS/Xray-core', '--info', 'depends:ca-bundle'])
    run([apk, '--keys-dir', ib / 'keys', 'verify', xapk])
    provenance['xray'] = {'tag': xrelease['tag_name'], 'prerelease': xrelease['prerelease'],
                          'published_at': xrelease['published_at'], 'archive_sha256': sha(xb),
                          'binary_sha256': sha(binary), 'version_output': output.strip()}
    # Only theme/status user-space packages use the upstream signed feed.
    key = ib / 'keys/custom-feed.rsa.pub'
    get('https://qiusimons.github.io/OpenWrt-Add/keys/custom-feed.rsa.pub', key)
    repo = 'https://qiusimons.github.io/OpenWrt-Add/aarch64_generic/packages.adb'
    with (ib / 'repositories').open('a') as f:
        f.write('\n' + repo + '\n')
    provenance['custom_feed_key_sha256'] = sha(key)
    overlay = work / 'overlay/etc/apk/keys'
    overlay.mkdir(parents=True, exist_ok=True)
    shutil.copy2(public, overlay / public.name)
    shutil.copy2(key, overlay / key.name)
    # Keep language choice on upgrades; initialize only a missing option.
    init = work / 'overlay/etc/uci-defaults/90-r2s-language'
    init.parent.mkdir(exist_ok=True)
    init.write_text("#!/bin/sh\nuci -q get luci.main.lang >/dev/null || { uci set luci.main.lang='zh_cn'; uci commit luci; }\nexit 0\n")
    init.chmod(0o755)
    tokens = [line.strip() for line in (ROOT / 'profiles/packages.r2s').read_text().splitlines() if line.strip() and not line.startswith('#')]
    packages_arg = ' '.join(tokens)
    manifest_text = run(['make', '-C', ib, 'manifest', 'PROFILE=' + POLICY['profile'], 'PACKAGES=' + packages_arg], capture=True)
    print(manifest_text, flush=True)
    (evidence / 'resolved-manifest.log').write_text(manifest_text)
    resolved = parse_manifest(manifest_text)
    required = verify_packages(resolved, hard_deps, xv)
    provenance['resolved_packages'] = resolved
    (evidence / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    if not build:
        print('Official R2S dependencies verified; firmware not assembled.')
        return
    run(['make', '-C', ib, 'image', 'PROFILE=' + POLICY['profile'], 'PACKAGES=' + packages_arg,
         'FILES=' + str(work / 'overlay'), 'ROOTFS_PARTSIZE=822', 'KERNEL_PARTSIZE=32'])
    target = ib / 'bin/targets/rockchip/armv8'
    manifests = list(target.glob('*nanopi-r2s*.manifest'))
    require(len(manifests) == 1, 'Expected one R2S manifest')
    installed = parse_manifest(manifests[0].read_text())
    verify_packages(installed, hard_deps, xv)
    require(installed == resolved, 'Installed image manifest differs from resolved package set')
    for name in ('config.buildinfo', 'feeds.buildinfo', 'version.buildinfo'):
        if (target / name).exists():
            shutil.copy2(target / name, evidence / name)
    shutil.copy2(manifests[0], evidence / manifests[0].name)
    artifact = ROOT / 'artifact'
    artifact.mkdir(exist_ok=True)
    date = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
    checksums = []
    images = []
    for fs, suffix in [('ext4', 'ext4'), ('squashfs', 'sfs')]:
        candidates = list(target.glob('*nanopi-r2s-' + fs + '-sysupgrade.img.gz'))
        require(len(candidates) == 1, 'Missing or ambiguous ' + fs + ' image')
        image = candidates[0]
        raw = work / (fs + '.img')
        tail = inflate(image, raw)
        if tail.strip(b'\0'):
            meta = evidence / (fs + '-upgrade-metadata.json')
            run([ib / 'staging_dir/host/bin/fwtool', '-i', meta, image])
            metadata = json.loads(meta.read_text())
            require('friendlyarm,nanopi-r2s' in metadata.get('supported_devices', []), 'Wrong upgrade device metadata')
        parts = check_layout(raw, fs)
        if fs == 'squashfs':
            rootfs = work / 'audit-rootfs'
            run(['unsquashfs', '-no-progress', '-processors', '2', '-offset', str(parts[1]['start'] * 512), '-d', rootfs, raw])
            require(sha(rootfs / 'usr/bin/xray') == sha(binary), 'Xray in filesystem differs from authenticated release')
            db = (rootfs / 'lib/apk/db/installed').read_text()
            actual = {}
            for block in db.split('\n\n'):
                d = dict((line[:1], line[2:]) for line in block.splitlines() if line.startswith(('P:', 'V:')))
                if 'P' in d:
                    actual[d['P']] = d['V']
            require(actual == installed, 'Actual APK database differs from manifest')
            for driver in ('r8152.ko', 'nft_tproxy.ko', 'nft_socket.ko'):
                require(list((rootfs / 'lib/modules').rglob(driver)), 'Driver absent from rootfs: ' + driver)
            for path in ('sbin/init', 'sbin/procd', 'sbin/fw4', 'usr/sbin/nginx', 'usr/bin/chinadns-ng', 'etc/init.d/passwall2', 'sbin/sysupgrade'):
                require((rootfs / path).exists() or (rootfs / path).is_symlink(), 'Critical file absent: ' + path)
            dns_version = run(['timeout', '30', 'qemu-aarch64-static', '-L', rootfs, rootfs / 'usr/sbin/dnsmasq', '--version'], capture=True)
            require('nftset' in dns_version.split() and 'no-nftset' not in dns_version.split(), 'DNSmasq lacks nftset support')
            (evidence / 'dnsmasq-version.txt').write_text(dns_version)
            assets = rootfs / 'usr/share/v2ray'
            for name in ('geoip.dat', 'geosite.dat'):
                require((assets / name).is_file(), 'Geo data missing: ' + name)
            config = work / 'xray-geo-test.json'
            config.write_text(json.dumps({'log': {'loglevel': 'none'}, 'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}],
                                         'routing': {'rules': [{'type': 'field', 'ip': ['geoip:cn'], 'outboundTag': 'direct'},
                                                               {'type': 'field', 'domain': ['geosite:cn'], 'outboundTag': 'direct'}]}}))
            previous_assets = os.environ.get('XRAY_LOCATION_ASSET')
            os.environ['XRAY_LOCATION_ASSET'] = str(assets)
            try:
                test_result = run(['timeout', '30', 'qemu-aarch64-static', rootfs / 'usr/bin/xray', 'run', '-test', '-config', config], capture=True)
                (evidence / 'xray-geo-test.txt').write_text(test_result)
            finally:
                if previous_assets is None:
                    os.environ.pop('XRAY_LOCATION_ASSET', None)
                else:
                    os.environ['XRAY_LOCATION_ASSET'] = previous_assets
            # Syntax only: no target application or router service is started.
            for path in (rootfs / 'usr/share/passwall2').rglob('*.lua'):
                run(['luac5.1', '-p', path])
        zipname = f'R2S-GC404-{date}-{version}-{suffix}.zip'
        with zipfile.ZipFile(artifact / zipname, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(image, image.name)
        digest = sha(image)
        checksums.append(digest + '  ' + image.name + '\n')
        images.append({'image': image.name, 'zip': zipname, 'sha256': digest, 'zip_sha256': sha(artifact / zipname),
                       'image_bytes': raw.stat().st_size, 'raw_sha256': sha(raw), 'partitions': parts,
                       'gzip_crc': 'passed', 'fwtool_trailer_bytes': len(tail)})
    (artifact / f'R2S-GC404-{date}-{version}.sha256sum').write_text(''.join(checksums))
    report = {'commit': os.environ.get('GITHUB_SHA'), 'openwrt': version, 'expected_xray': xv,
              'kernel_policy': provenance['kernel_policy'], 'packages': installed, 'required': required,
              'disabled_count': len(POLICY['disabled']), 'images': images,
              'checks': ['Official ImageBuilder checksum', 'Authenticated third-party APKs', 'APK dependency resolution',
                         'Required and disabled package gates', 'Xray latest release and ARM64 version',
                         'Installed manifest versus solved manifest', 'ZIP and gzip CRC', 'MBR and retained partition layout',
                         'Squashfs all-file extraction', 'Actual APK database', 'Network driver files', 'Lua syntax',
                         'DNSmasq nftset support', 'Xray China GeoIP/GeoSite configuration test under QEMU'],
              'hardware_validation': 'not performed'}
    (evidence / 'r2s-verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print('R2S official-kernel candidate verified:', len(installed), 'packages;', len(required), 'protected;', xv, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true')
    assemble(parser.parse_args().build)
