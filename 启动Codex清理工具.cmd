@echo off
chcp 65001 >nul
title Codex-Session-Cleaner_LZH

if exist "%~dp0Codex-Session-Cleaner_LZH.exe" (
    "%~dp0Codex-Session-Cleaner_LZH.exe" %*
    set "tool_exit=%ERRORLEVEL%"
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_console.ps1" %*
    set "tool_exit=%ERRORLEVEL%"
)

if not "%tool_exit%"=="0" (
    echo.
    echo Tool exited with code: %tool_exit%
    pause
)
exit /b %tool_exit%
