"""Recheck immutable build backup before publishing its two ZIP assets."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

root = Path(sys.argv[1])
reports = list(root.rglob('r2s-verification.json'))
assert len(reports) == 1, 'Missing/ambiguous image verification report'
report = json.loads(reports[0].read_text())
assert report['commit'] == sys.argv[2], 'Artifact belongs to another source commit'
autocore = report.get('autocore', {})
assert autocore.get('mode') == 'display-only' and autocore.get('network_tuning') == 'disabled'
stub = Path(__file__).resolve().parents[1] / 'profiles/r2s-overlay/etc/init.d/autocore'
assert autocore.get('init_sha256') == hashlib.sha256(stub.read_bytes()).hexdigest(), 'Wrong autocore service in image'
assert len(report['images']) == 2
assert {x['image'].split('-r2s-')[1].split('-')[0] for x in report['images']} == {'ext4', 'squashfs'}
for image in report['images']:
    files = list(root.rglob(image['zip']))
    assert len(files) == 1
    p = files[0]
    assert hashlib.sha256(p.read_bytes()).hexdigest() == image['zip_sha256']
    with zipfile.ZipFile(p) as z:
        assert z.testzip() is None and z.namelist() == [image['image']]
        assert hashlib.sha256(z.read(image['image'])).hexdigest() == image['sha256']
print('Backup ZIPs, image checksums and source commit verified before release')
