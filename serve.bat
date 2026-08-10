@echo off
cd /d "%~dp0"

chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist "%~dp0.venv\Scripts\aether.exe" (
  call "%~dp0run.bat" doctor
)
echo Starting AETHER API at http://127.0.0.1:7420
echo An API token will be printed below - the dashboard link includes it.
echo Dashboard: cd web ^&^& npm install ^&^& npm run build   (then it is served at / above)
echo.
"%~dp0.venv\Scripts\aether.exe" serve
pause
