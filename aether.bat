@echo off
REM Drop-in terminal launcher — use from anywhere if you add this folder to PATH,
REM or double-click / call:  aether.bat
cd /d "%~dp0"
if not exist ".venv\Scripts\aether.exe" (
  echo Installing AETHER...
  python -m venv .venv
  call .venv\Scripts\pip install -e ".[dev]" -q
)
call .venv\Scripts\activate.bat
if "%~1"=="" (
  aether
) else (
  aether %*
)
