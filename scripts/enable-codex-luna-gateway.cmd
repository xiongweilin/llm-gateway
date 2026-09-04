@echo off
setlocal
set "PWSH=%USERPROFILE%\scoop\apps\pwsh\current\pwsh.exe"
if not exist "%PWSH%" set "PWSH=pwsh.exe"
"%PWSH%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0enable-codex-luna-gateway.ps1" %*
exit /b %ERRORLEVEL%
