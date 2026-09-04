@echo off
setlocal

set "PWSH=%USERPROFILE%\scoop\apps\pwsh\current\pwsh.exe"
if not exist "%PWSH%" set "PWSH=pwsh.exe"

"%PWSH%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0switch-codex-to-official.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" echo 切换失败，保留上面的错误和备份路径。
if "%EXIT_CODE%"=="0" echo 切换完成，请现在完全退出并重新打开 Codex。
pause
exit /b %EXIT_CODE%
