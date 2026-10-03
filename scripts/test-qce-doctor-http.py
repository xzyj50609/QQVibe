"""Exercise real Windows PowerShell HTTP parsing against synthetic loopback services."""
import hashlib
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'synthetic-secret-do-not-export'
ACCOUNT = '123456789'


def run():
    calls = []
    scenario = {'mode': 'online'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.handle_api()

        def do_GET(self):
            self.handle_api()

        def handle_api(self):
            calls.append((self.command, self.path))
            payload = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            mode = scenario['mode']
            if mode == 'redirect':
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/should-not-follow')
                self.end_headers()
                return
            if mode == 'malformed':
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'not-json')
                return
            if self.path == '/api/auth/login':
                expected = hashlib.sha256((SECRET + '.napcat').encode()).hexdigest()
                assert json.loads(payload)['hash'] == expected
                value = {'code': 0, 'data': {'Credential': SECRET}} if mode != 'bad-auth' else {'code': 1, 'message': SECRET}
            elif self.path == '/api/QQLogin/CheckLoginStatus':
                assert self.headers.get('Authorization') == 'Bearer ' + SECRET
                value = {'code': 0, 'data': {'isLogin': mode == 'online'}}
            elif self.path == '/api/QQLogin/GetQQLoginQrcode':
                value = {'code': 0, 'data': {'qrcode': 'synthetic-qr-do-not-export'}} if mode != 'missing-qr' else {'code': 0, 'data': {}}
            elif self.path in ('/api/system/status', '/api/system/info'):
                if mode == 'qce-auth':
                    self.send_response(401)
                    self.end_headers()
                    return
                value = {'success': True, 'data': {'loggedIn': mode == 'online'}}
                if self.path == '/api/system/info':
                    value['data'] = {'version': '6.3.0', 'napcat': {'version': '4.18.19', 'online': mode == 'online', 'selfInfo': {'uin': ACCOUNT}}}
                    if mode == 'wrong-shape':
                        value['data'] = {'random': SECRET}
            else:
                raise AssertionError(f'unexpected or private route: {self.path}')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='qqvibe-doctor-http-') as temp:
            work = Path(temp)
            install = work / 'QCE 测试 空格目录'
            (install / 'config').mkdir(parents=True)
            (install / '.qce-config').mkdir()
            (install / 'config/webui.json').write_text(json.dumps({'enable': True, 'port': 6099, 'token': SECRET}), encoding='utf-8')
            (install / '.qce-config/security.json').write_text(json.dumps({'accessToken': SECRET}), encoding='utf-8')
            for name in ('launcher-user.bat', 'NapCatWinBootMain.exe', 'NapCatWinBootHook.dll', 'napcat.mjs', 'loadNapCat.js', 'qqnt.json', 'qce-server.exe'):
                (install / name).write_text('synthetic-never-executed', encoding='utf-8')
            ps = work / 'http-test.ps1'
            core = str(ROOT / 'scripts/qce-doctor-core.ps1').replace("'", "''")
            directory = str(install).replace("'", "''")
            ps.write_text(f"""$ErrorActionPreference='Stop'
. '{core}'
$script:RealDoctorHTTP=(Get-Command Invoke-DoctorHTTP).ScriptBlock
function Invoke-DoctorHTTP([int]$Port,[string]$Path,[string]$Method='GET',$Body=$null,[string]$Bearer='') {{
    & $script:RealDoctorHTTP {server.server_port} $Path $Method $Body $Bearer
}}
function Get-DoctorOpenPorts {{ return @(6099,40653) }}
function Get-DoctorQQCandidates {{ return @() }}
function Get-CimInstance {{ return @() }}
$snapshot=Get-QCEDoctorSnapshot '{directory}'
$report=ConvertTo-QCEDoctorReport $snapshot
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
[Console]::WriteLine(($report|ConvertTo-Json -Depth 16))
""", encoding='utf-8-sig')
            powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            expected = {'online': 'online', 'qr': 'login-required', 'missing-qr': 'needs-attention', 'bad-auth': 'needs-attention',
                        'qce-auth': 'login-required', 'wrong-shape': 'login-required', 'malformed': 'needs-attention', 'redirect': 'needs-attention'}
            for mode, outcome in expected.items():
                scenario['mode'] = mode
                result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(ps)], capture_output=True, timeout=25)
                assert result.returncode == 0, result.stderr.decode(errors='replace')
                text = result.stdout.decode('utf-8-sig')
                report = json.loads(text)
                assert report['outcome'] == outcome, (mode, report)
                assert SECRET not in text and ACCOUNT not in text and 'synthetic-qr-do-not-export' not in text
                checks = {row['id']: row for row in report['checks']}
                if mode == 'online':
                    assert checks['qce-api']['state'] == 'pass'
                    assert report['versions']['qce'] == '6.3.0'
                if mode in ('wrong-shape', 'malformed', 'redirect'):
                    assert checks['qce-api']['state'] == 'fail'
                if mode == 'bad-auth':
                    assert checks['webui-auth']['state'] == 'fail'
                print(f'PASS: actual PowerShell HTTP {mode}', flush=True)
            assert '/should-not-follow' not in [path for _, path in calls]
            assert all(path in ('/api/auth/login', '/api/QQLogin/CheckLoginStatus', '/api/QQLogin/GetQQLoginQrcode', '/api/system/info', '/api/system/status') for _, path in calls)
            print('QCE_DOCTOR_HTTP_TESTS_PASSED: 8 scenarios; fixed routes; redacted reports', flush=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    run()
