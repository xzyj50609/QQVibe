"""Build the dependency-free standalone Windows QCE doctor, without user data."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    '检查并修复QCE.cmd': 'scripts/qce-doctor.cmd',
    'qce-doctor.ps1': 'scripts/qce-doctor.ps1',
    'qce-doctor-core.ps1': 'scripts/qce-doctor-core.ps1',
    '使用说明.md': 'docs/public/QCE-DOCTOR.md',
    'LICENSE': 'LICENSE',
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build(output):
    output = output.absolute()
    for part in (output, *output.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('linked output is refused')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'QCE-检测修复工具-1.0.0.zip'
    if archive.exists():
        raise ValueError('keep the old delivery; use a fresh output directory')
    blobs = {name: (ROOT / source).read_bytes() for name, source in FILES.items()}
    for name in ('qce-doctor.ps1', 'qce-doctor-core.ps1'):
        if not blobs[name].startswith(b'\xef\xbb\xbf'):
            raise ValueError('PowerShell 5.1 requires BOM for Chinese source')
    blobs['SHA256SUMS.txt'] = ''.join(f'{digest(data)}  {name}\n' for name, data in sorted(blobs.items())).encode('utf-8-sig')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for name, data in sorted(blobs.items()):
            target.writestr(name, data)
    with zipfile.ZipFile(archive) as source:
        assert source.testzip() is None
        assert set(source.namelist()) == set(blobs)
        assert all(source.read(name) == data for name, data in blobs.items())
    # Real extraction and real Windows PowerShell entry; do not repair developer QCE.
    with tempfile.TemporaryDirectory(prefix='qqvibe-doctor-package-') as temp:
        extracted = Path(temp)
        with zipfile.ZipFile(archive) as source:
            source.extractall(extracted)
        report_file = extracted / 'package-check.json'
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(extracted / 'qce-doctor.ps1'), '-NoGui', '-InstallDirectory',
                                 str(extracted / 'missing-qce'), '-ReportPath', str(report_file)],
                                timeout=30, capture_output=True)
        if result.returncode != 0:
            raise ValueError('extracted standalone entry failed')
        check = json.loads(report_file.read_text(encoding='utf-8'))
        assert check['schema'] == 'qce-doctor-v1' and len(check['checks']) == 17
        assert check['outcome'] == 'needs-attention' and not check['repairs']
    report = {'schema': 'qce-doctor-delivery-v1', 'version': '1.0.0', 'zip': archive.name,
              'bytes': archive.stat().st_size, 'sha256': digest(archive.read_bytes()), 'verifiedFiles': len(blobs),
              'extractedPowerShell51Entry': True, 'externalComputerAccepted': False,
              'files': {name: digest(data) for name, data in blobs.items()}}
    (output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    shutil.copy2(archive, output / 'QCE-Doctor-1.0.0.zip')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'QQVibeData/releases/QCE-Doctor-1.0.0-20261003')
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False, indent=2))
