@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHON=%ROOT%\runtime\python\python.exe"
if not exist "%PYTHON%" if defined WECHATVIBE_PYTHON set "PYTHON=%WECHATVIBE_PYTHON%"
if not exist "%PYTHON%" set "PYTHON=python"
"%PYTHON%" "%ROOT%\scripts\start-real-client.py" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" if "%~1"=="" pause
exit /b %RESULT%
