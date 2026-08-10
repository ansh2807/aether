@echo off
REM Drop-in launcher. Works from any directory: every path is anchored at this
REM script's own folder, so it does not depend on the shell searching the
REM current directory (which NoDefaultCurrentDirectoryInExePath=1 disables).
cd /d "%~dp0"

chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist "%~dp0.venv\Scripts\aether.exe" (
  echo Installing AETHER...
  python -m venv "%~dp0.venv"
  call "%~dp0.venv\Scripts\pip.exe" install -e ".[dev]" -q
)

if "%~1"=="" (
  "%~dp0.venv\Scripts\aether.exe"
) else (
  "%~dp0.venv\Scripts\aether.exe" %*
)
