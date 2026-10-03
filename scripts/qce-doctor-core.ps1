# Windows PowerShell 5.1, built-in .NET only. Never return credentials, account IDs or raw logs.
Set-StrictMode -Version 2.0
$script:DoctorVersion = '1.0.0'

function Get-DoctorField($Object, [string]$Name, $Default = $null) {
    if ($Object -is [Collections.IDictionary]) { if($Object.Contains($Name)){return $Object[$Name]};return $Default }
    if ($null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]) { return $Object.$Name }
    return $Default
}

function Test-DoctorSafePath([string]$Path) {
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path) -or $Path.StartsWith('\\')) { return $false }
    try {
        $current = [IO.Path]::GetFullPath($Path)
        while ($current) {
            if (Test-Path -LiteralPath $current) {
                $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
            }
            $current = Split-Path -Parent $current
        }
        return $true
    } catch { return $false }
}

function Read-DoctorJSON([string]$Path) {
    if (-not (Test-DoctorSafePath $Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        if ((Get-Item -LiteralPath $Path).Length -gt 65536) { return $null }
        return ([IO.File]::ReadAllText($Path) | ConvertFrom-Json -ErrorAction Stop)
    } catch { return $null }
}

function Get-DoctorQQCandidates {
    $found = New-Object 'System.Collections.Generic.List[string]'
    foreach ($key in @('HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\QQ.exe',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\QQ.exe')) {
        try {
            $entry = Get-ItemProperty -LiteralPath $key -ErrorAction Stop
            $location = Get-DoctorField $entry 'InstallLocation'
            if ($location) { $found.Add((Join-Path $location 'QQ.exe')) }
            $icon = Get-DoctorField $entry 'DisplayIcon'
            if ($icon) { $found.Add(($icon -replace ',\d+$','').Trim('"')) }
            $default = Get-DoctorField $entry '(default)'
            if ($default) { $found.Add($default.Trim('"')) }
            $uninstall = Get-DoctorField $entry 'UninstallString'
            if ($uninstall -and $uninstall -match '^"?(.+?Uninstall\.exe)"?$') {
                $found.Add((Join-Path (Split-Path -Parent $Matches[1]) 'QQ.exe'))
            }
        } catch { }
    }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if ($root) {
            $found.Add((Join-Path $root 'Tencent\QQNT\QQ.exe'))
            $found.Add((Join-Path $root 'Tencent\QQ\QQ.exe'))
        }
    }
    if ($env:LOCALAPPDATA) { $found.Add((Join-Path $env:LOCALAPPDATA 'Programs\Tencent\QQNT\QQ.exe')) }
    try {
        foreach ($proc in @(Get-CimInstance Win32_Process -Filter "Name='QQ.exe'" -OperationTimeoutSec 3 -ErrorAction Stop)) {
            if ($proc.ExecutablePath) { $found.Add($proc.ExecutablePath) }
        }
    } catch { }
    return @($found | Select-Object -Unique)
}

function Get-DoctorQQMetadata([string]$Executable) {
    if (-not (Test-DoctorSafePath $Executable) -or -not (Test-Path -LiteralPath $Executable -PathType Leaf) -or
        [IO.Path]::GetFileName($Executable) -ine 'QQ.exe' -or $Executable -match '\\QQ\\Bin\\QQ\.exe$') { return $null }
    $root = Split-Path -Parent $Executable
    $packages = @((Join-Path $root 'resources\app\package.json'))
    $versions = Join-Path $root 'versions'
    if (Test-DoctorSafePath $versions) {
        $packages += @(Get-ChildItem -LiteralPath $versions -Directory -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 10 |
            ForEach-Object { Join-Path $_.FullName 'resources\app\package.json' })
    }
    foreach ($package in $packages) {
        $data = Read-DoctorJSON $package
        if ($null -ne $data) {
            $version = [string](Get-DoctorField $data 'version' '')
            $build = [string](Get-DoctorField $data 'buildVersion' '')
            if ($version -notmatch '^[0-9][0-9.\-]{0,40}$') { $version = 'unknown' }
            if ($build -notmatch '^\d{1,10}$') { $build = '' }
            return [pscustomobject]@{ Executable = [IO.Path]::GetFullPath($Executable); Version = $version; Build = $build; Package = $package }
        }
    }
    return $null
}

function Find-DoctorInstall([string]$Requested) {
    if ($Requested) { return $Requested }
    $state = if ($env:APPDATA) { Read-DoctorJSON (Join-Path $env:APPDATA 'qce-installer\state.json') }
    $saved = [string](Get-DoctorField $state 'installDir' '')
    if ($saved -and (Test-DoctorSafePath $saved) -and (Test-Path -LiteralPath $saved -PathType Container)) { return $saved }
    if ($env:LOCALAPPDATA) { return (Join-Path $env:LOCALAPPDATA 'QQChatExporter') }
    return ''
}

function Invoke-DoctorHTTP([int]$Port, [string]$Path, [string]$Method = 'GET', $Body = $null, [string]$Bearer = '') {
    # Fixed loopback, no system proxy, redirects, credentials in URL, or unbounded reads.
    if ($Port -lt 1 -or $Port -gt 65535 -or $Path -notin @('/api/auth/login', '/api/QQLogin/CheckLoginStatus',
        '/api/QQLogin/GetQQLoginQrcode', '/api/system/info', '/api/system/status')) { return @{ Kind = 'invalid' } }
    $response = $null
    $deadline=[DateTime]::UtcNow.AddSeconds(4)
    try {
        $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port$Path")
        $request.Proxy = $null; $request.AllowAutoRedirect = $false
        $request.Timeout = 1800; $request.ReadWriteTimeout = 1800; $request.Method = $Method
        if ($Bearer) { $request.Headers['Authorization'] = 'Bearer ' + $Bearer }
        if ($null -ne $Body) {
            $bytes = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Compress -Depth 5))
            $request.ContentType = 'application/json'; $request.ContentLength = $bytes.Length
            $stream = $request.GetRequestStream(); try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
        }
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200) { return @{ Kind = 'http-error' } }
        $stream = $response.GetResponseStream(); $buffer = New-Object byte[] 65537; $used = 0
        try {
            while ($used -lt $buffer.Length) {
                if([DateTime]::UtcNow -gt $deadline){$request.Abort();return @{Kind='unavailable'}}
                $size = $stream.Read($buffer, $used, $buffer.Length - $used)
                if([DateTime]::UtcNow -gt $deadline){$request.Abort();return @{Kind='unavailable'}}
                if ($size -eq 0) { break }; $used += $size
            }
        } finally { $stream.Dispose() }
        if ($used -gt 65536) { return @{ Kind = 'invalid' } }
        $value = [Text.Encoding]::UTF8.GetString($buffer, 0, $used) | ConvertFrom-Json -ErrorAction Stop
        return @{ Kind = 'ok'; Data = $value }
    } catch [Net.WebException] {
        $response = $_.Exception.Response
        if ($response -and [int]$response.StatusCode -in @(401,403)) { return @{ Kind = 'auth' } }
        return @{ Kind = 'unavailable' }
    } catch { return @{ Kind = 'invalid' } } finally { if ($response) { $response.Dispose() } }
}

function Get-DoctorOpenPorts([int[]]$Ports) {
    $pending = @(); $open = @()
    try {
        foreach ($port in ($Ports | Select-Object -Unique)) {
            if ($port -lt 1 -or $port -gt 65535) { continue }
            $tcp = New-Object Net.Sockets.TcpClient
            $pending += [pscustomobject]@{ Port = $port; Client = $tcp; Async = $tcp.BeginConnect('127.0.0.1',$port,$null,$null) }
        }
        # One shared deadline; unreachable ports cannot multiply total timeout.
        $deadline = [DateTime]::UtcNow.AddMilliseconds(600)
        foreach ($probe in $pending) {
            $remaining = [Math]::Max(0, [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds)
            if ($probe.Async.AsyncWaitHandle.WaitOne($remaining)) {
                try { $probe.Client.EndConnect($probe.Async); if ($probe.Client.Connected) { $open += $probe.Port } } catch { }
            }
        }
    } finally { foreach ($probe in $pending) { $probe.Client.Dispose(); $probe.Async.AsyncWaitHandle.Dispose() } }
    return $open
}

function Get-DoctorLogEvidence([string]$Path) {
    # Only the tail of the latest run. Historical failures cannot trigger repairs.
    $result = @{ Code = 'unavailable'; Since = $null }
    if (-not (Test-DoctorSafePath $Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $result }
    $stream = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $start = [Math]::Max(0, $stream.Length - 65536); [void]$stream.Seek($start,[IO.SeekOrigin]::Begin)
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
        try { $tail = $reader.ReadToEnd() } finally { $reader.Dispose() }
        $markers = [regex]::Matches($tail, '(?m)^.*(?:\[installer\] starting service|\[launcher\] starting).*$')
        if ($markers.Count -eq 0 -or (Get-Item -LiteralPath $Path).LastWriteTimeUtc -lt [DateTime]::UtcNow.AddMinutes(-15)) {
            return @{Code='stale-log';Since=(Get-Item -LiteralPath $Path).LastWriteTimeUtc.ToString('o')}
        }
        $tail = $tail.Substring($markers[$markers.Count-1].Index)
        $code = 'no-recognized-error'
        if ($tail -match '(?i)(vcruntime140|msvcp140).*?(not found|missing|找不到|无法找到)|找不到.*?(vcruntime140|msvcp140)') { $code = 'vc-runtime-missing' }
        elseif ($tail -match '(?i)Access is denied|拒绝访问|Permission denied') { $code = 'access-denied' }
        elseif ($tail -match '(?i)Incompatible QQ|QQ Installation Not Found|Could not detect QQ|QQ路径.*无效|QQ path.*invalid') { $code = 'qq-path-invalid' }
        elseif ($tail -match '(?i)ERR_MODULE_NOT_FOUND|Cannot find module|找不到指定的模块') { $code = 'component-missing' }
        elseif ($tail -match '(?i)未找到对应版本的偏移|版本兼容性不佳|获取Appid异常') { $code = 'version-incompatible' }
        elseif ($tail -match '(?i)等待网络连接|waiting for network') {
            if ($tail -notmatch '网络已连接|network connected') { $code = 'network-waiting' }
        }
        $result.Code = $code; $result.Since = (Get-Item -LiteralPath $Path).LastWriteTimeUtc.ToString('o')
        return $result
    } catch { return $result } finally { if ($stream) { $stream.Dispose() } }
}

function New-DoctorCheck([string]$Id,[string]$Title,[string]$State,[string]$Message,[string]$Repair = '') {
    return [pscustomobject][ordered]@{ id=$Id; title=$Title; state=$State; message=$Message; repair=$Repair }
}

function Get-QCEDoctorSnapshot([string]$InstallDirectory = '', [string]$QQExecutable = '') {
    $dir = Find-DoctorInstall $InstallDirectory
    $safe = (Test-DoctorSafePath $dir) -and (Test-Path -LiteralPath $dir -PathType Container)
    $savedPath = ''; $savedQQ = $null; $qq = $null; $web = $null; $security = $null; $missing = @(); $mode='unknown'
    if ($safe) {
        $mode=if(Test-Path -LiteralPath (Join-Path $dir 'napiLoader.bat') -PathType Leaf){'framework'}else{'shell'}
        $pathFile = Join-Path $dir 'config\qq_path.txt'
        if ((Test-DoctorSafePath $pathFile) -and (Test-Path -LiteralPath $pathFile -PathType Leaf) -and (Get-Item -LiteralPath $pathFile).Length -lt 8192) {
            $savedPath = [IO.File]::ReadAllText($pathFile).Trim(); $savedQQ = Get-DoctorQQMetadata $savedPath
        }
        $web = Read-DoctorJSON (Join-Path $dir 'config\webui.json')
        $required=if($mode -eq 'framework'){@('napiLoader.bat')}else{@('launcher-user.bat','NapCatWinBootMain.exe','NapCatWinBootHook.dll','napcat.mjs','loadNapCat.js','qqnt.json','qce-server.exe')}
        foreach ($name in $required) {
            $file = Join-Path $dir $name
            if (-not (Test-DoctorSafePath $file) -or -not (Test-Path -LiteralPath $file -PathType Leaf) -or (Get-Item -LiteralPath $file).Length -eq 0) { $missing += $name }
        }
        $security = Read-DoctorJSON (Join-Path $dir '.qce-config\security.json')
        if (-not $security -and $env:USERPROFILE) { $security = Read-DoctorJSON (Join-Path $env:USERPROFILE '.qq-chat-exporter\security.json') }
    }
    $candidates = @()
    if ($QQExecutable) { $candidates += $QQExecutable }
    if ($savedQQ) { $candidates += $savedQQ.Executable }
    $candidates += @(Get-DoctorQQCandidates)
    foreach ($candidate in ($candidates | Select-Object -Unique)) { $qq = Get-DoctorQQMetadata $candidate; if ($qq) { break } }
    $processes = @(); $processReadable = $true
    try { $processes = @(Get-CimInstance Win32_Process -Filter "Name='NapCatWinBootMain.exe' OR Name='QQ.exe' OR Name='qce-server.exe'" -OperationTimeoutSec 3 -ErrorAction Stop) }
    catch { $processReadable = $false }
    $ownBoot = @($processes | Where-Object { $_.ExecutablePath -and $safe -and $_.ExecutablePath -ieq (Join-Path $dir 'NapCatWinBootMain.exe') })
    $unknownBoot = @($processes | Where-Object { $_.Name -ieq 'NapCatWinBootMain.exe' -and (-not $_.ExecutablePath -or -not $safe -or $_.ExecutablePath -ine (Join-Path $dir 'NapCatWinBootMain.exe')) })
    $configuredPort = Get-DoctorField $web 'port' 6099
    if ($configuredPort -isnot [int] -and $configuredPort -isnot [long]) { $configuredPort = 6099 }
    if ($configuredPort -lt 1 -or $configuredPort -gt 65535) { $configuredPort = 6099 }
    $ports = @(Get-DoctorOpenPorts (@([int]$configuredPort) + @(6099..6118) + @(40653)))
    $login = 'unavailable'; $qr = 'unknown'; $actualPort = $null; $credential = ''
    $webToken = [string](Get-DoctorField $web 'token' '')
    $triedAuth = $false
    if ($webToken.Length -gt 0 -and $webToken.Length -le 4096) {
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $hash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($webToken + '.napcat')))).Replace('-','').ToLowerInvariant() }
        finally { $sha.Dispose() }
        foreach ($port in @($ports | Where-Object { $_ -ne 40653 } | Select-Object -First 3)) {
            $triedAuth = $true
            $auth = Invoke-DoctorHTTP $port '/api/auth/login' 'POST' @{hash=$hash}
            if ($auth.Kind -ne 'ok') { continue }
            $data = Get-DoctorField $auth.Data 'data'
            $credential = [string](Get-DoctorField $data 'Credential' (Get-DoctorField $data 'credential' ''))
            if (-not $credential -or $credential.Length -gt 8192 -or (Get-DoctorField $auth.Data 'code' -1) -ne 0) { $credential=''; continue }
            $actualPort = $port
            $response = Invoke-DoctorHTTP $port '/api/QQLogin/CheckLoginStatus' 'POST' @{} $credential
            if ($response.Kind -eq 'ok' -and (Get-DoctorField $response.Data 'code' -1) -eq 0) {
                $data = Get-DoctorField $response.Data 'data'
                $isLogin = Get-DoctorField $data 'isLogin' (Get-DoctorField $data 'online')
                if ($isLogin -is [bool]) {
                    $login = if ($isLogin) { 'online' } else { 'offline' }
                    if (-not $isLogin) {
                        $response = Invoke-DoctorHTTP $port '/api/QQLogin/GetQQLoginQrcode' 'POST' @{} $credential
                        $data = if ($response.Kind -eq 'ok' -and (Get-DoctorField $response.Data 'code' -1) -eq 0) { Get-DoctorField $response.Data 'data' }
                        $qr = 'missing'
                        foreach ($name in @('qrcode','qrcodeurl','qrcodeUrl','url')) {
                            if ((Get-DoctorField $data $name '') -is [string] -and (Get-DoctorField $data $name '').Length -gt 0) { $qr='available'; break }
                        }
                    } else { $qr='not-needed' }
                } else { $login='protocol-invalid' }
            } else { $login='protocol-invalid' }
            break
        }
        if (-not $actualPort -and $triedAuth) { $login='auth-or-service-invalid' }
    }
    $qce = 'unavailable'; $qceVersion = 'unknown'; $napcatVersion = 'unknown'; $qqVersion = if($qq){$qq.Version}else{'unknown'}
    $access = [string](Get-DoctorField $security 'accessToken' '')
    if (40653 -in $ports) {
        if (-not $access -or $access.Length -gt 4096 -or (Get-DoctorField $security 'tokenExpired' $false) -eq $true) { $qce='credential-missing' }
        else {
            $status = Invoke-DoctorHTTP 40653 '/api/system/status' 'GET' $null $access
            $info = Invoke-DoctorHTTP 40653 '/api/system/info' 'GET' $null $access
            if ($status.Kind -eq 'auth' -or $info.Kind -eq 'auth') { $qce='auth-failed' }
            elseif ($status.Kind -eq 'ok' -and $info.Kind -eq 'ok') {
                $statusData = Get-DoctorField $status.Data 'data'; $infoData=Get-DoctorField $info.Data 'data'
                $nap = Get-DoctorField $infoData 'napcat'; $self = Get-DoctorField $nap 'selfInfo'
                $online = Get-DoctorField $nap 'online'
                if ((Get-DoctorField $status.Data 'success') -eq $true -and (Get-DoctorField $info.Data 'success') -eq $true -and
                    $online -is [bool] -and (-not $online -or ([string](Get-DoctorField $self 'uin' '') -match '^[1-9][0-9]{0,19}$')) -and
                    (-not $online -or (Get-DoctorField $statusData 'loggedIn' $true) -eq $true)) {
                    $qce = if($online){'online'}else{'offline'}
                    $qceVersion=[string](Get-DoctorField $infoData 'version' 'unknown')
                    $napcatVersion=[string](Get-DoctorField $nap 'version' 'unknown')
                    if($qceVersion -notmatch '^[vV]?[0-9][0-9.\-]{0,40}$'){$qceVersion='unknown'}
                    if($napcatVersion -notmatch '^[vV]?[0-9][0-9.\-]{0,40}$'){$napcatVersion='unknown'}
                } else { $qce='protocol-invalid' }
            } else { $qce='protocol-invalid' }
        }
    }
    $log = if($safe){Get-DoctorLogEvidence (Join-Path $dir 'logs\qce-runtime.log')}else{@{Code='unavailable';Since=$null}}
    $vc = @('vcruntime140.dll','msvcp140.dll') | Where-Object { -not (Test-Path -LiteralPath (Join-Path ([Environment]::GetFolderPath('System')) $_)) }
    $free = $null
    if ($safe) { try { $drive=New-Object IO.DriveInfo([IO.Path]::GetPathRoot($dir)); $free=$drive.AvailableFreeSpace } catch { } }
    return [pscustomobject]@{ InstallDirectory=$dir; Mode=$mode; SafeInstall=$safe; MissingComponents=@($missing); QQ=$qq; SavedQQ=$savedQQ;
        SavedPath=$savedPath; WebConfig=$web; Ports=$ports; WebPort=$actualPort; Login=$login; QR=$qr; QCE=$qce;
        QCEVersion=$qceVersion; NapCatVersion=$napcatVersion; QQVersion=$qqVersion; Log=$log; OwnBoot=$ownBoot;
        UnknownBoot=$unknownBoot; ProcessReadable=$processReadable; MissingVC=@($vc); FreeBytes=$free;
        NetworkAvailable=[Net.NetworkInformation.NetworkInterface]::GetIsNetworkAvailable(); Is64BitOS=[Environment]::Is64BitOperatingSystem;
        OSVersion=[Environment]::OSVersion.Version.ToString() }
}

function ConvertTo-QCEDoctorReport($Snapshot) {
    $s=$Snapshot; $checks=New-Object 'System.Collections.Generic.List[object]'
    $checks.Add((New-DoctorCheck 'system' '系统环境' $(if($s.Is64BitOS){'pass'}else{'fail'}) $(if($s.Is64BitOS){'64 位 Windows；具体系统与版本组合仍需实测。'}else{'当前工具支持的 QCE 路线需要 64 位 Windows。'})))
    $checks.Add((New-DoctorCheck 'installation' 'QCE 安装位置' $(if($s.SafeInstall){'pass'}else{'fail'}) $(if($s.SafeInstall){'找到本机普通安装目录。'}else{'未找到可检查的 QCE 安装；可点“选择 QCE 目录”。链接目录暂不写入修复。'})))
    $checks.Add((New-DoctorCheck 'mode' '启动方式' $(if($s.Mode -eq 'unknown'){'unknown'}else{'pass'}) $(if($s.Mode -eq 'framework'){'检测到 Framework；需通过 napiLoader 启动桌面 QQ，本工具不自动中断桌面 QQ。'}elseif($s.Mode -eq 'shell'){'检测到 Shell 安装；在线登录应启动 launcher-user，standalone 只用于离线浏览。'}else{'尚未确认安装方式。'})))
    $components = if(-not $s.SafeInstall){'unknown'}elseif($s.MissingComponents.Count){'fail'}elseif($s.Mode -eq 'framework'){'unknown'}else{'pass'}
    $checks.Add((New-DoctorCheck 'components' '后台组件' $components $(if($components -eq 'pass'){'Shell 启动所需的关键文件存在；这不替代完整发布包校验。'}elseif($components -eq 'fail'){'缺少或无法检查：'+($s.MissingComponents -join '、')+'。需要从官方完整包补齐，不自动覆盖安装。'}elseif($s.Mode -eq 'framework'){'已找到 Framework 入口，其他插件文件布局尚无统一核验清单，不判为完整通过。'}else{'等待确认安装目录。'})))
    $checks.Add((New-DoctorCheck 'qq' 'QQNT 安装' $(if($s.QQ){'pass'}else{'fail'}) $(if($s.QQ){'找到具有 QQNT 文件结构的 QQ。'}else{'未找到 QQNT；可点“选择 QQ.exe”。旧 QQ/TIM 不适用于此路线。'})))
    $pathRepair = if($s.SafeInstall -and $s.Mode -eq 'shell' -and $s.QQ -and (-not $s.SavedQQ -or $s.QQ.Executable -ine $s.SavedQQ.Executable)){'fix-qq-path'}else{''}
    $checks.Add((New-DoctorCheck 'qq-path' '后台使用的 QQ 路径' $(if($pathRepair){'fail'}elseif($s.SavedQQ){'pass'}else{'unknown'}) $(if($pathRepair){'保存路径需要更新为当前确认的 QQNT；修复前保留原路径。'}elseif($s.SavedQQ){'已保存路径指向 QQNT。'}else{'尚未确认后台保存的 QQNT 路径，暂不修改。'}) $pathRepair))
    $webBad = $null -ne $s.WebConfig -and (Get-DoctorField $s.WebConfig 'enable' $true) -eq $false
    $port = Get-DoctorField $s.WebConfig 'port' 6099
    $portBad = ($port -isnot [int] -and $port -isnot [long]) -or $port -lt 1 -or $port -gt 65535
    $webRepair = if($s.SafeInstall -and $s.WebConfig -and ($webBad -or $portBad) -and -not $s.OwnBoot.Count -and -not $s.UnknownBoot.Count -and $s.ProcessReadable){'fix-webui-config'}else{''}
    $webConfigState = if(-not $s.WebConfig){'unknown'}elseif($webBad -or $portBad){'fail'}else{'pass'}
    $checks.Add((New-DoctorCheck 'webui-config' '登录服务配置' $webConfigState $(if($webConfigState -eq 'pass'){'登录服务配置可读取。'}elseif($webConfigState -eq 'fail'){'登录服务被关闭或端口无效；后台停止时可备份后修正。'}else{'未找到有效配置；保留原文件，不生成或替换 token。'}) $webRepair))
    $webPorts=@($s.Ports | Where-Object {$_ -ne 40653})
    $runtimeState=if($s.WebPort){'pass'}elseif(-not $webPorts.Count){'fail'}else{'warn'}
    $canStart=$s.SafeInstall -and $s.Mode -eq 'shell' -and $s.Is64BitOS -and -not $s.MissingComponents.Count -and $s.QQ -and $s.ProcessReadable -and
        -not $s.OwnBoot.Count -and -not $s.UnknownBoot.Count -and -not $s.WebPort -and -not $webPorts.Count -and $s.WebConfig -and
        [string](Get-DoctorField $s.WebConfig 'token' '') -and $s.Log.Code -notin @('component-missing','version-incompatible','access-denied','vc-runtime-missing')
    $checks.Add((New-DoctorCheck 'runtime' 'NapCat 后台与端口' $runtimeState $(if($s.WebPort){'已确认 NapCat 登录服务，可连接。'}elseif(-not $webPorts.Count){'未连到登录服务。后台可能没启动、已经退出或未就绪；不能归因于二维码图片。'}else{'有端口响应，但尚未确认是当前安装的 NapCat 登录服务。'}) $(if($canStart){'start-runtime'}else{''})))
    $checks.Add((New-DoctorCheck 'webui-auth' '登录服务认证' $(if($s.WebPort){'pass'}elseif($s.Login -eq 'auth-or-service-invalid'){'fail'}else{'unknown'}) $(if($s.WebPort){'使用本机配置完成认证，凭证仅留在内存。'}elseif($s.Login -eq 'auth-or-service-invalid'){'认证失败或端口属于其他服务；不自动清空 token。'}else{'登录服务不可用，尚不能验证认证。'})))
    $checks.Add((New-DoctorCheck 'login' 'QQ 登录状态' $(if($s.Login -eq 'online'){'pass'}elseif($s.Login -eq 'offline'){'action'}else{'unknown'}) $(if($s.Login -eq 'online'){'QQ 已登录。'}elseif($s.Login -eq 'offline'){'后台已经就绪，请在 QCE 完成扫码或快捷登录。'}else{'尚未确认 QQ 登录状态。'})))
    $checks.Add((New-DoctorCheck 'qr' '二维码获取' $(if($s.QR -in @('available','not-needed')){'pass'}elseif($s.QR -eq 'missing'){'fail'}else{'unknown'}) $(if($s.QR -eq 'available'){'后台已提供二维码；请在 QCE 登录窗口扫码。报告不保存二维码。'}elseif($s.QR -eq 'not-needed'){'账号已登录，无需二维码。'}elseif($s.QR -eq 'missing'){'登录服务可连接，但还没提供二维码；需结合网络和最新启动错误。'}else{'前面的登录服务尚未通过，无法检查二维码。'})))
    $qceState=if($s.QCE -eq 'online'){'pass'}elseif($s.QCE -in @('offline','credential-missing')){'action'}elseif($s.QCE -eq 'unavailable'){'unknown'}else{'fail'}
    $checks.Add((New-DoctorCheck 'qce-api' 'QCE 导出接口' $qceState $(switch($s.QCE){
        'online'{'认证、状态和信息接口已通过；QQVibe 可以进一步连接。'}
        'offline'{'导出接口可访问，QQ 尚未在线。'}
        'credential-missing'{'导出端口存在，但本机受控凭证尚未准备好。'}
        'auth-failed'{'QCE 接口认证失败；需核对安装来源与凭证，不自动重置。'}
        'protocol-invalid'{'返回结构不符合已验证的 QCE 接口；端口响应不等于连接正常。'}
        default{'导出接口尚不可用；登录完成后重新检测。'}})))
    $checks.Add((New-DoctorCheck 'network' '本机网络' $(if($s.NetworkAvailable){'pass'}else{'warn'}) $(if($s.NetworkAvailable){'系统报告网络可用；不代表 QQ 登录服务器一定可达。'}else{'系统报告没有可用网络，需先恢复网络。'})))
    $checks.Add((New-DoctorCheck 'vc-runtime' 'VC++ 运行库线索' $(if($s.Log.Code -eq 'vc-runtime-missing'){'fail'}elseif($s.MissingVC.Count){'warn'}else{'pass'}) $(if($s.Log.Code -eq 'vc-runtime-missing'){'最新日志明确提示 VC++ DLL 缺失，可打开微软安装入口。'}elseif($s.MissingVC.Count){'系统目录未找到部分 DLL；程序可能自带运行库，不能据此判定启动失败。'}else{'系统目录中存在常用运行库 DLL。'})))
    $checks.Add((New-DoctorCheck 'disk' '安装盘可用空间' $(if($null -eq $s.FreeBytes){'unknown'}elseif($s.FreeBytes -lt 200MB){'warn'}else{'pass'}) $(if($null -eq $s.FreeBytes){'无法确认安装盘空间。'}elseif($s.FreeBytes -lt 200MB){'安装盘剩余空间较少，可能影响启动或写日志。'}else{'安装盘至少有 200 MB 可用空间；未据此承诺完整安装空间足够。'})))
    $logMessage=switch($s.Log.Code){
        'version-incompatible'{'最近启动日志提示 QQ/NapCat 版本兼容问题；不自动升级或降级 QQ。'}
        'component-missing'{'最近启动日志提示模块或组件缺失。'}
        'access-denied'{'最近启动日志提示访问被拒绝；保留现有权限，需确认具体文件。'}
        'vc-runtime-missing'{'最近启动日志提示 VC++ DLL 缺失。'}
        'qq-path-invalid'{'最近启动日志提示 QQ 路径或版本不适用。'}
        'network-waiting'{'最近启动日志停在等待网络。'}
        'unavailable'{'尚无可读启动日志。'}
        'stale-log'{'现有日志较旧或没有启动标记，不用历史错误推断本次启动失败。'}
        default{'最近启动日志未匹配到已知错误；这不等于后台正常。'}}
    $checks.Add((New-DoctorCheck 'startup-log' '最近启动错误' $(if($s.Log.Code -in @('unavailable','stale-log','no-recognized-error')){'unknown'}else{'warn'}) $logMessage))
    $checks.Add((New-DoctorCheck 'compatibility' '版本组合' $(if($s.QCEVersion -in @('6.3.0','v6.3.0')){'pass'}else{'unknown'}) $(if($s.QCEVersion -in @('6.3.0','v6.3.0')){'QCE 为当前 QQVibe 已验证接口版本；其他电脑运行仍以实测为准。'}else{'未确认 QCE 6.3.0，暂不把其他版本视为已验证。'})))
    $repairs=@($checks | Where-Object repair | ForEach-Object repair | Select-Object -Unique)
    $summary=if($s.QCE -eq 'online'){'QCE 已在线，接口验证通过。'}elseif($s.QR -eq 'available'){'后台和二维码已就绪，请回 QCE 扫码登录。'}elseif($repairs.Count){'检测到可执行的最小修复，修复后将重新检测。'}else{'仍有未通过的关口，请查看具体结果；没有足够证据时不改配置。'}
    return [pscustomobject][ordered]@{schema='qce-doctor-v1';toolVersion=$script:DoctorVersion;generatedAt=[DateTime]::UtcNow.ToString('o');
        environment=@{osVersion=$s.OSVersion;is64BitOS=$s.Is64BitOS;installMode=$s.Mode};versions=@{qq=$s.QQVersion;napcat=$s.NapCatVersion;qce=$s.QCEVersion};
        summary=$summary;checks=$checks.ToArray();repairs=$repairs;outcome=$(if($s.QCE -eq 'online'){'online'}elseif($s.QR -eq 'available'){'login-required'}else{'needs-attention'});
        logEvidence=@{code=$s.Log.Code;lastWrite=$s.Log.Since}}
}

function Write-DoctorConfig([string]$Path,[string]$Content) {
    if (-not (Test-DoctorSafePath $Path)) { throw 'unsafe-path' }
    $parent=Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { [void][IO.Directory]::CreateDirectory($parent) }
    $backup=$null
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $backup=$Path+'.doctor-backup-'+[DateTime]::UtcNow.ToString('yyyyMMddHHmmssfff')+'-'+[guid]::NewGuid().ToString('N')
        [IO.File]::Copy($Path,$backup,$false)
    }
    $temp=$Path+'.doctor-new-'+[guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllText($temp,$Content,(New-Object Text.UTF8Encoding($false)))
        if(Test-Path -LiteralPath $Path -PathType Leaf){[IO.File]::Replace($temp,$Path,$backup)}else{[IO.File]::Move($temp,$Path)}
    } finally { if(Test-Path -LiteralPath $temp){Remove-Item -LiteralPath $temp -Force} }
    return $backup
}

function Start-DoctorRuntime($Snapshot) {
    $dir=$Snapshot.InstallDirectory
    # Launcher uses cmd expansion. Decline paths that cannot safely survive its batch semantics.
    if ($dir -match '[%!^\r\n"]' -or $Snapshot.QQ.Executable -match '[%!^\r\n"]' -or -not (Test-DoctorSafePath $dir)) { throw 'launcher-path-unsupported' }
    $launcher=Join-Path $dir 'launcher-user.bat'
    if (-not (Test-DoctorSafePath $launcher)) { throw 'unsafe-launcher' }
    $logs=Join-Path $dir 'logs'; if(-not(Test-DoctorSafePath $logs)){throw 'unsafe-logs'}
    [void][IO.Directory]::CreateDirectory($logs)
    $log=Join-Path $logs 'qce-runtime.log'; if(-not(Test-DoctorSafePath $log)){throw 'unsafe-log'}
    $info=New-Object Diagnostics.ProcessStartInfo
    $info.FileName=Join-Path ([Environment]::GetFolderPath('System')) 'cmd.exe'
    $info.Arguments='/d /s /c ""'+$launcher+'" 1>>"'+$log+'" 2>&1"'
    $info.WorkingDirectory=$dir; $info.UseShellExecute=$false; $info.CreateNoWindow=$true
    $info.RedirectStandardInput=$true
    $info.EnvironmentVariables['NAPCAT_QQ_PATH']=$Snapshot.QQ.Executable
    $info.EnvironmentVariables['QCE_CONFIG_DIR']=Join-Path $dir '.qce-config'
    $info.EnvironmentVariables['QCE_LOG_DIR']=$logs
    $info.EnvironmentVariables['QCE_LOG_FILE']=$log
    $info.EnvironmentVariables['QCE_STDIO_CAPTURED']='1'
    $info.EnvironmentVariables['QCE_NO_AUTO_OPEN']='1'
    $info.EnvironmentVariables['NAPCAT_HIDE_CONSOLE']='1'
    [void]$info.EnvironmentVariables.Remove('QCE_STANDALONE_MODE')
    $process=[Diagnostics.Process]::Start($info); $process.StandardInput.Close()
    return $process
}

function Invoke-QCEDoctorRepair($Snapshot,[scriptblock]$StartRuntime = {param($s) Start-DoctorRuntime $s},
    [scriptblock]$Refresh = {param($s) $exe=if($s.QQ){$s.QQ.Executable}else{''}; Get-QCEDoctorSnapshot $s.InstallDirectory $exe}) {
    $report=ConvertTo-QCEDoctorReport $Snapshot; $results=New-Object 'System.Collections.Generic.List[object]'
    foreach($action in $report.repairs) {
        try {
            $fresh=& $Refresh $Snapshot
            if(-not $fresh -or $action -notin (ConvertTo-QCEDoctorReport $fresh).repairs) {
                $results.Add([pscustomobject]@{action=$action;state='skipped';message='环境已变化，此项不再适用；已跳过并继续复检。'})
                continue
            }
            $Snapshot=$fresh
            switch($action) {
                'fix-qq-path' {
                    $verified=Get-DoctorQQMetadata $Snapshot.QQ.Executable
                    if(-not $verified){throw 'qq-changed'}
                    [void](Write-DoctorConfig (Join-Path $Snapshot.InstallDirectory 'config\qq_path.txt') ($verified.Executable+"`r`n"))
                    $Snapshot.SavedQQ=$verified
                    $message='已备份原 QQ 路径，并保存已确认的 QQNT 路径。'
                }
                'fix-webui-config' {
                    $file=Join-Path $Snapshot.InstallDirectory 'config\webui.json'; $config=Read-DoctorJSON $file
                    if(-not $config){throw 'config-changed'}
                    $config | Add-Member -NotePropertyName enable -NotePropertyValue $true -Force
                    $port=Get-DoctorField $config 'port' 6099
                    if(($port -isnot [int] -and $port -isnot [long]) -or $port -lt 1 -or $port -gt 65535){$config | Add-Member -NotePropertyName port -NotePropertyValue 6099 -Force}
                    [void](Write-DoctorConfig $file ($config | ConvertTo-Json -Depth 12))
                    $message='已备份登录配置并启用登录服务；原 token 和其他配置保留。'
                }
                'start-runtime' {
                    $runtime=& $StartRuntime $Snapshot
                    if(-not $runtime){throw 'launch-failed'}
                    $message='已发起后台启动，是否恢复以接下来的复检为准。'
                }
            }
            $results.Add([pscustomobject]@{action=$action;state='applied';message=$message})
        } catch {
            $results.Add([pscustomobject]@{action=$action;state='failed';message='此项未完成；原配置备份保留，需检查权限或文件是否变化。'})
            break
        }
    }
    return $results.ToArray()
}
