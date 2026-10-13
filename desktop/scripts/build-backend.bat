@echo off
setlocal
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0build-backend.ps1" %*
exit /b %errorlevel%
