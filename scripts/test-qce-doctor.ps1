$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'qce-doctor-core.ps1')
$script:Passed=0
function Assert-Doctor($Condition,[string]$Name){if(-not $Condition){throw ('FAILED: '+$Name)};$script:Passed++;Write-Output ('PASS: '+$Name)}
$root=Join-Path ([IO.Path]::GetTempPath()) ('qqvibe-doctor-test-'+[guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($root)
try{
    $install=Join-Path $root 'QCE with spaces';$qqroot=Join-Path $root 'QQNT';$qqpath=Join-Path $qqroot 'QQ.exe'
    [void][IO.Directory]::CreateDirectory((Join-Path $qqroot 'resources\app'))
    [IO.File]::WriteAllText($qqpath,'synthetic-executable-never-run')
    [IO.File]::WriteAllText((Join-Path $qqroot 'resources\app\package.json'),'{"version":"9.9.36-53644","buildVersion":"53644"}')
    [void][IO.Directory]::CreateDirectory((Join-Path $install 'config'))
    $webpath=Join-Path $install 'config\webui.json';$qqconfig=Join-Path $install 'config\qq_path.txt'
    [IO.File]::WriteAllText($webpath,'{"enable":false,"port":-1,"token":"synthetic-secret-token","custom":"preserve-me"}')
    [IO.File]::WriteAllText($qqconfig,'invalid-old-path')
    $qq=Get-DoctorQQMetadata $qqpath
    $s=[pscustomobject]@{InstallDirectory=$install;Mode='shell';SafeInstall=$true;MissingComponents=@();QQ=$qq;SavedQQ=$null;SavedPath='invalid';
        WebConfig=(Read-DoctorJSON $webpath);Ports=@();WebPort=$null;Login='unavailable';QR='unknown';QCE='unavailable';QCEVersion='unknown';
        NapCatVersion='unknown';QQVersion='9.9.36-53644';Log=@{Code='no-recognized-error';Since=$null};OwnBoot=@();UnknownBoot=@();ProcessReadable=$true;
        MissingVC=@();FreeBytes=3GB;NetworkAvailable=$true;Is64BitOS=$true;OSVersion='10.0.22631.0'}
    $r=ConvertTo-QCEDoctorReport $s
    Assert-Doctor ($r.checks.Count -eq 17) 'every connection gate is represented'
    Assert-Doctor ($r.repairs.Count -eq 3) 'only path, disabled WebUI and absent runtime produce repairs'
    $json=$r|ConvertTo-Json -Depth 16
    Assert-Doctor ($json -notmatch 'synthetic-secret-token|preserve-me|invalid-old-path|QQNT\\|QCE with spaces') 'report excludes token, config content and local paths'
    Assert-Doctor ($r.outcome -eq 'needs-attention') 'missing listener is never a successful connection'
    $script:Started=0
    $refresh={param($old) $old.SavedQQ=Get-DoctorQQMetadata (([IO.File]::ReadAllText($qqconfig)).Trim());$old.WebConfig=Read-DoctorJSON $webpath;return $old}
    $actions=@(Invoke-QCEDoctorRepair $s {param($old) $script:Started++;$old.OwnBoot=@([pscustomobject]@{ProcessId=123});return [pscustomobject]@{Id=123}} $refresh)
    Assert-Doctor ($script:Started -eq 1 -and $actions.Count -eq 3) 'one scoped startup follows both confirmed config repairs'
    $fixed=Read-DoctorJSON $webpath
    Assert-Doctor ($fixed.enable -eq $true -and $fixed.port -eq 6099 -and $fixed.token -eq 'synthetic-secret-token' -and $fixed.custom -eq 'preserve-me') 'repair preserves token and unrelated settings'
    Assert-Doctor (([IO.File]::ReadAllText($qqconfig)).Trim() -eq $qqpath) 'QQ path points to verified QQNT'
    $backups=@(Get-ChildItem -LiteralPath (Join-Path $install 'config') -Filter '*.doctor-backup-*')
    Assert-Doctor ($backups.Count -eq 2) 'both original config files have recoverable backups'
    Assert-Doctor (@($backups|Where-Object{[IO.File]::ReadAllText($_.FullName) -eq 'invalid-old-path'}).Count -eq 1) 'old path is preserved byte for byte'
    $again=@(Invoke-QCEDoctorRepair $s {throw 'must-not-start-again'} $refresh)
    Assert-Doctor ($again.Count -eq 0 -and $script:Started -eq 1) 'repeated repair does not launch another backend'
    $s.OwnBoot=@();$s.UnknownBoot=@([pscustomobject]@{ProcessId=42})
    Assert-Doctor ('start-runtime' -notin (ConvertTo-QCEDoctorReport $s).repairs) 'unknown NapCat instance prevents startup'
    $s.UnknownBoot=@();$s.ProcessReadable=$false
    Assert-Doctor ('start-runtime' -notin (ConvertTo-QCEDoctorReport $s).repairs) 'unreadable process ownership prevents startup'
    $s.ProcessReadable=$true;$s.MissingComponents=@('NapCatWinBootHook.dll')
    Assert-Doctor ('start-runtime' -notin (ConvertTo-QCEDoctorReport $s).repairs) 'missing component prevents futile startup'
    $s.MissingComponents=@();$s.Log.Code='version-incompatible'
    Assert-Doctor ('start-runtime' -notin (ConvertTo-QCEDoctorReport $s).repairs) 'confirmed version mismatch is not retried or downgraded'
    $s.Log.Code='no-recognized-error';$s.Mode='framework';$s.SavedQQ=$null
    Assert-Doctor ('fix-qq-path' -notin (ConvertTo-QCEDoctorReport $s).repairs -and 'start-runtime' -notin (ConvertTo-QCEDoctorReport $s).repairs) 'Framework mode is not treated as a Shell repair'
    $s.Mode='shell';$s.SavedQQ=$qq;$s.QCE='online';$s.WebPort=6099;$s.Login='online';$s.QR='not-needed';$s.QCEVersion='6.3.0'
    $online=ConvertTo-QCEDoctorReport $s
    Assert-Doctor ($online.outcome -eq 'online' -and $online.repairs.Count -eq 0) 'fully online path has no repair'
    $s.QCE='protocol-invalid';$s.Login='offline';$s.QR='available'
    Assert-Doctor ((ConvertTo-QCEDoctorReport $s).outcome -eq 'login-required') 'QR readiness does not imply QCE connection'
    $s.WebPort=$null;$s.QR='unknown';$s.Ports=@(6099);$s.Login='auth-or-service-invalid'
    Assert-Doctor ('start-runtime' -notin (ConvertTo-QCEDoctorReport $s).repairs) 'auth failure is not mistaken for absent service'
    $s.Ports=@();$s.WebConfig | Add-Member enable $false -Force
    $race=@(Invoke-QCEDoctorRepair $s {throw 'never launch'} {param($old)$old.WebPort=6099;$old.WebConfig.enable=$true;return $old})
    Assert-Doctor (@($race|Where-Object state -eq 'applied').Count -eq 0) 'fresh state cancels now-inapplicable repairs'
    $log=Join-Path $install 'runtime-test.log'
    [IO.File]::WriteAllText($log,"[installer] starting service`nAccess is denied`n[launcher] starting`n网络已连接")
    Assert-Doctor ((Get-DoctorLogEvidence $log).Code -eq 'no-recognized-error') 'old run errors do not contaminate latest run'
    [IO.File]::WriteAllText($log,"[launcher] starting`nCould not detect QQ installation automatically.")
    Assert-Doctor ((Get-DoctorLogEvidence $log).Code -eq 'qq-path-invalid') 'latest path failure is classified without exporting raw logs'
    (Get-Item -LiteralPath $log).LastWriteTimeUtc=[DateTime]::UtcNow.AddHours(-1)
    Assert-Doctor ((Get-DoctorLogEvidence $log).Code -eq 'stale-log') 'stale log cannot trigger present repair'
    Assert-Doctor (-not(Test-DoctorSafePath '\\server\share\file') -and -not(Test-DoctorSafePath 'relative\file')) 'remote and relative mutation paths are refused'
    $link=Join-Path $root 'linked';[void](New-Item -ItemType Junction -Path $link -Target $install)
    Assert-Doctor (-not(Test-DoctorSafePath (Join-Path $link 'config\webui.json'))) 'junction traversal is refused before mutation'
    [IO.Directory]::Delete($link,$false)
    $closedPortListener=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0);$closedPortListener.Start()
    $testPort=$closedPortListener.LocalEndpoint.Port
    Assert-Doctor ($testPort -in @(Get-DoctorOpenPorts @($testPort))) 'real loopback listener detection works'
    $closedPortListener.Stop()
    Assert-Doctor (@(Get-DoctorOpenPorts @($testPort)).Count -eq 0) 'closed port reproduces screenshot failure gate'
    $s.QQ=$qq;$s.InstallDirectory=$install
    $launcher=Join-Path $install 'launcher-user.bat'
    [IO.File]::WriteAllText($launcher,"@echo off`r`necho %NAPCAT_QQ_PATH% >`"%~dp0startup-proof.txt`"`r`nexit /b 0`r`n",[Text.Encoding]::ASCII)
    $proc=Start-DoctorRuntime $s
    Assert-Doctor ($proc.WaitForExit(5000) -and $proc.ExitCode -eq 0) 'real hidden launcher starts correctly with spaced directory'
    Assert-Doctor (([IO.File]::ReadAllText((Join-Path $install 'startup-proof.txt'))).Trim() -eq $qqpath) 'trusted QQ path reaches child environment without a shell argument'
    $proc.Dispose()
    Write-Output ('QCE_DOCTOR_TESTS_PASSED: '+$script:Passed)
}finally{
    $absolute=[IO.Path]::GetFullPath($root)
    $temp=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\'
    if($absolute.StartsWith($temp,[StringComparison]::OrdinalIgnoreCase) -and [IO.Path]::GetFileName($absolute).StartsWith('qqvibe-doctor-test-')){
        Remove-Item -LiteralPath $absolute -Recurse -Force
    }
}
