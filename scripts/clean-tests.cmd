@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0clean-tests.ps1"
set "cleanExit=%errorlevel%"
pause
exit /b %cleanExit%
