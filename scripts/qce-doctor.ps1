param(
    [switch]$NoGui, [switch]$Repair,
    [string]$InstallDirectory = '', [string]$QQExecutable = '', [string]$ReportPath = '',
    [string]$PreviewPath = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'qce-doctor-core.ps1')

function Invoke-DoctorRun {
    $before=Get-QCEDoctorSnapshot $InstallDirectory $QQExecutable
    $report=ConvertTo-QCEDoctorReport $before
    $initialReport=$report
    $actions=@()
    if($Repair -and $report.repairs.Count) {
        $actions=@(Invoke-QCEDoctorRepair $before)
        if(@($actions | Where-Object { $_.action -eq 'start-runtime' -and $_.state -eq 'applied' }).Count) {
            $deadline=[DateTime]::UtcNow.AddSeconds(35)
            do {
                Start-Sleep -Milliseconds 1200
                $ports=@(Get-DoctorOpenPorts (@(6099..6118) + @([int](Get-DoctorField $before.WebConfig 'port' 6099))))
                if($ports.Count){break}
            } while([DateTime]::UtcNow -lt $deadline)
        }
        $after=Get-QCEDoctorSnapshot $InstallDirectory $QQExecutable
        $report=ConvertTo-QCEDoctorReport $after
        # Rechecking, not launching, is the sole source of the recovery claim.
        foreach($action in $actions) {
            $id=switch($action.action){'fix-qq-path'{'qq-path'} 'fix-webui-config'{'webui-config'} 'start-runtime'{'runtime'}}
            $check=@($report.checks | Where-Object id -eq $id)[0]
            $action | Add-Member -NotePropertyName verified -NotePropertyValue ($action.state -eq 'applied' -and $check.state -eq 'pass')
        }
        $report | Add-Member -NotePropertyName before -NotePropertyValue @{
            outcome=$initialReport.outcome;checks=@($initialReport.checks)
        }
    }
    $report | Add-Member -NotePropertyName repairResults -NotePropertyValue $actions
    return $report
}

if($NoGui) {
    $mutex=$null;$ownsLock=$false
    try {
        if($Repair){
            $dir=Find-DoctorInstall $InstallDirectory
            $sha=[Security.Cryptography.SHA256]::Create()
            try{$key=([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($dir.ToLowerInvariant())))).Replace('-','')}finally{$sha.Dispose()}
            $mutex=New-Object Threading.Mutex($false,('Local\QQVibeDoctor-'+$key))
            try{$ownsLock=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$ownsLock=$true}
            if(-not $ownsLock){throw 'repair-already-running'}
        }
        $report=Invoke-DoctorRun
    } catch {
        $report=[pscustomobject]@{schema='qce-doctor-v1';toolVersion=$script:DoctorVersion;outcome='failed';summary='检测未完成；可尝试选择 QCE 目录后重试。';checks=@();repairs=@();repairResults=@()}
    } finally{if($ownsLock){$mutex.ReleaseMutex()};if($mutex){$mutex.Dispose()}}
    $json=$report | ConvertTo-Json -Depth 16
    if($ReportPath) {
        if(-not(Test-DoctorSafePath $ReportPath)){exit 2}
        [IO.File]::WriteAllText($ReportPath,$json,(New-Object Text.UTF8Encoding($false)))
    } else { [Console]::OutputEncoding=New-Object Text.UTF8Encoding($false); [Console]::WriteLine($json) }
    if($report.outcome -eq 'failed'){exit 1};exit 0
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$form=New-Object Windows.Forms.Form
$form.Text='QQVibe · QCE 检测与修复';$form.Size=New-Object Drawing.Size(850,690)
$form.MinimumSize=New-Object Drawing.Size(750,600);$form.StartPosition='CenterScreen'
$form.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$form.BackColor=[Drawing.Color]::White
$layout=New-Object Windows.Forms.TableLayoutPanel
$layout.Dock='Fill';$layout.ColumnCount=1;$layout.RowCount=5;$layout.Padding=New-Object Windows.Forms.Padding(20)
[void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle('Absolute',58)))
[void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle('Absolute',56)))
[void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle('Percent',100)))
[void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle('Absolute',44)))
[void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle('Absolute',38)))
$form.Controls.Add($layout)
$heading=New-Object Windows.Forms.Label
$heading.Text="检查 QQ 安装、后台启动、登录服务与 QCE 连接。`n修复只处理检测到的问题；原配置会备份，结果以复检为准。"
$heading.Dock='Fill';$layout.Controls.Add($heading,0,0)
$status=New-Object Windows.Forms.Label;$status.Dock='Fill';$status.Text='准备检测…';$status.ForeColor=[Drawing.Color]::FromArgb(40,85,135)
$layout.Controls.Add($status,0,1)
$list=New-Object Windows.Forms.ListView;$list.View='Details';$list.FullRowSelect=$true;$list.GridLines=$false;$list.Dock='Fill'
[void]$list.Columns.Add('关口',160);[void]$list.Columns.Add('结果',85);[void]$list.Columns.Add('说明（双击查看完整内容）',490)
$layout.Controls.Add($list,0,2)
$buttons=New-Object Windows.Forms.FlowLayoutPanel;$buttons.Dock='Fill';$buttons.WrapContents=$false
$layout.Controls.Add($buttons,0,3)
function Add-DoctorButton([string]$Text,[int]$Width) {
    $button=New-Object Windows.Forms.Button;$button.Text=$Text;$button.Width=$Width;$button.Height=32
    [void]$buttons.Controls.Add($button);return $button
}
$scan=Add-DoctorButton '重新检测' 92
$fix=Add-DoctorButton '最小修复并复检' 145
$chooseInstall=Add-DoctorButton '选择 QCE 目录' 135
$chooseQQ=Add-DoctorButton '选择 QQ.exe' 125
$save=Add-DoctorButton '保存匿名报告' 132
$foot=New-Object Windows.Forms.Label;$foot.Dock='Fill';$foot.Text='不会关闭你的桌面 QQ、删除数据、重置 token 或自动更换 QQ 版本。';$foot.Font=New-Object Drawing.Font('Microsoft YaHei UI',9);$foot.ForeColor=[Drawing.Color]::Gray
$layout.Controls.Add($foot,0,4)
$script:DoctorReport=$null;$script:Worker=$null;$script:WorkerFile=$null;$script:WorkerStarted=$null
$script:SelectedInstall=$InstallDirectory;$script:SelectedQQ=$QQExecutable
$script:RepairRequested=$false
$labels=@{pass='通过';fail='未通过';warn='需注意';unknown='未确认';action='需登录'}

function Start-DoctorWorker([bool]$DoRepair) {
    if($script:Worker){return}
    $workerDir=Join-Path $env:LOCALAPPDATA 'QQVibeDoctor'
    if(-not(Test-DoctorSafePath $workerDir)){throw 'unsafe-report-directory'}
    [void][IO.Directory]::CreateDirectory($workerDir)
    $script:WorkerFile=Join-Path $workerDir ('check-'+[guid]::NewGuid().ToString('N')+'.json')
    $info=New-Object Diagnostics.ProcessStartInfo
    $info.FileName=Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    $argsList=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',('"'+$PSCommandPath+'"'),'-NoGui','-ReportPath',('"'+$script:WorkerFile+'"'))
    foreach($pair in @(@('-InstallDirectory',$script:SelectedInstall),@('-QQExecutable',$script:SelectedQQ))) {
        if($pair[1]){if($pair[1] -match '["\r\n]'){throw 'invalid-path'};$argsList+=@($pair[0],('"'+$pair[1]+'"'))}
    }
    if($DoRepair){$argsList+='-Repair'}
    $info.Arguments=$argsList -join ' ';$info.UseShellExecute=$false;$info.CreateNoWindow=$true
    $script:Worker=[Diagnostics.Process]::Start($info);$script:WorkerStarted=[DateTime]::UtcNow
    $script:RepairRequested=$DoRepair
    foreach($button in @($scan,$fix,$chooseInstall,$chooseQQ,$save)){$button.Enabled=$false}
    $status.Text=if($DoRepair){'正在执行最小修复并复检。后台首次启动最多等待 35 秒…'}else{'正在逐项检测，通常需要几秒。此过程不会修改 QCE 配置…'}
}

function Show-DoctorReport($Report) {
    $script:DoctorReport=$Report;$list.Items.Clear()
    foreach($check in $Report.checks){
        $row=New-Object Windows.Forms.ListViewItem($check.title)
        [void]$row.SubItems.Add($labels[$check.state]);[void]$row.SubItems.Add($check.message)
        $row.Tag=$check.message
        if($check.state -eq 'fail'){$row.ForeColor=[Drawing.Color]::Firebrick}
        elseif($check.state -in @('unknown','warn','action')){$row.ForeColor=[Drawing.Color]::FromArgb(145,100,20)}
        [void]$list.Items.Add($row)
    }
    $status.Text=$Report.summary
    if($script:RepairRequested){
        $verified=@($Report.repairResults | Where-Object { $_.verified -eq $true }).Count
        $status.Text="复检完成：$verified 项修复已验证。"+$Report.summary
    }
    $fix.Enabled=$Report.repairs.Count -gt 0
    $save.Enabled=$true
}

$timer=New-Object Windows.Forms.Timer;$timer.Interval=250
$timer.Add_Tick({
    if(-not $script:Worker){return}
    if(-not $script:Worker.HasExited -and ([DateTime]::UtcNow-$script:WorkerStarted).TotalSeconds -lt 110){return}
    if(-not $script:Worker.HasExited){$script:Worker.Kill();$status.Text='检测超时，未确认恢复；请重新检测。'}
    else {
        $result=Read-DoctorJSON $script:WorkerFile
        if($result -and (Get-DoctorField $result 'schema') -eq 'qce-doctor-v1'){
            Show-DoctorReport $result
            if($PreviewPath -and (Test-DoctorSafePath $PreviewPath)){
                $bitmap=New-Object Drawing.Bitmap($form.Width,$form.Height)
                try{$form.DrawToBitmap($bitmap,(New-Object Drawing.Rectangle(0,0,$form.Width,$form.Height)));$bitmap.Save($PreviewPath,[Drawing.Imaging.ImageFormat]::Png)}finally{$bitmap.Dispose()}
                $form.Close()
            }
        }
        else{$status.Text='检测未完成；请重新检测或选择 QCE 安装目录。'}
    }
    if($script:Worker){$script:Worker.Dispose();$script:Worker=$null}
    foreach($button in @($scan,$chooseInstall,$chooseQQ)){$button.Enabled=$true}
})
$scan.Add_Click({Start-DoctorWorker $false})
$fix.Add_Click({Start-DoctorWorker $true})
$chooseInstall.Add_Click({
    $picker=New-Object Windows.Forms.FolderBrowserDialog;$picker.Description='选择含 launcher-user.bat 的 QCE 安装文件夹'
    if($picker.ShowDialog($form) -eq 'OK'){$script:SelectedInstall=$picker.SelectedPath;Start-DoctorWorker $false};$picker.Dispose()
})
$chooseQQ.Add_Click({
    $picker=New-Object Windows.Forms.OpenFileDialog;$picker.Title='选择当前使用的 QQNT 程序';$picker.Filter='QQ 程序|QQ.exe'
    if($picker.ShowDialog($form) -eq 'OK'){$script:SelectedQQ=$picker.FileName;Start-DoctorWorker $false};$picker.Dispose()
})
$save.Add_Click({
    if(-not $script:DoctorReport){return}
    $picker=New-Object Windows.Forms.SaveFileDialog;$picker.FileName='QCE-匿名检测报告.json';$picker.Filter='JSON 报告|*.json'
    if($picker.ShowDialog($form) -eq 'OK'){
        try {
            if(-not(Test-DoctorSafePath $picker.FileName)){throw 'unsafe-output'}
            [IO.File]::WriteAllText($picker.FileName,($script:DoctorReport | ConvertTo-Json -Depth 16),(New-Object Text.UTF8Encoding($false)))
            $status.Text='匿名报告已保存：不含聊天、QQ 号、token、二维码和原始日志。'
        }catch{$status.Text='报告未能保存，请选择普通可写目录。'}
    };$picker.Dispose()
})
$list.Add_DoubleClick({if($list.SelectedItems.Count){[void][Windows.Forms.MessageBox]::Show($form,[string]$list.SelectedItems[0].Tag,'检测说明')}})
$form.Add_Shown({$timer.Start();Start-DoctorWorker $false})
$form.Add_FormClosed({$timer.Stop();$timer.Dispose();if($script:Worker -and -not $script:Worker.HasExited){$script:Worker.Kill()}})
[void]$form.ShowDialog();$form.Dispose()
