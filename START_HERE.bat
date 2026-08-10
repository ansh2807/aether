@echo off
title AETHER Terminal
cd /d "%~dp0"

REM UTF-8 console so the UI renders instead of crashing on the legacy code page.
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

color 0B
echo.
echo   Starting AETHER terminal session...
echo   (login wizard + agent REPL, like Claude Code / Open Code)
echo.

if not exist "%~dp0.venv\Scripts\aether.exe" (
  call "%~dp0run.bat" doctor
)

"%~dp0.venv\Scripts\aether.exe"
echo.
pause
