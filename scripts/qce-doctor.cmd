@echo off
setlocal
set "DOCTOR_SCRIPT=%~dp0qce-doctor.ps1"
if exist "%~dp0resources\client\scripts\qce-doctor.ps1" set "DOCTOR_SCRIPT=%~dp0resources\client\scripts\qce-doctor.ps1"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File "%DOCTOR_SCRIPT%"
if errorlevel 1 (
  echo QCE doctor could not start. Keep this window and report this message.
  pause
)
