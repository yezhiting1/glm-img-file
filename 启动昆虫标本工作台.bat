@echo off
chcp 65001 >nul
setlocal

set "PORTABLE_ROOT=%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PORTABLE_ROOT%start-portable.ps1"
set "EXIT_CODE=%errorlevel%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo 启动失败，按任意键关闭窗口。
    pause >nul
)

exit /b %EXIT_CODE%
