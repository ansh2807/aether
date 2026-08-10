@echo off
setlocal
cd /d "%~dp0"

REM UTF-8 console. Without this the legacy code page cannot encode the UI
REM glyphs and Rich raises UnicodeEncodeError mid-command.
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM Every path below is anchored at this script's folder (%~dp0). Bare names
REM would fail where NoDefaultCurrentDirectoryInExePath=1 stops the shell
REM searching the current directory.
set "AETHER_EXE=%~dp0.venv\Scripts\aether.exe"

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [AETHER] Creating virtual environment...
  python -m venv "%~dp0.venv"
  if errorlevel 1 (
    echo Failed to create venv. Is Python installed?
    pause
    exit /b 1
  )
  echo [AETHER] Installing package...
  call "%~dp0.venv\Scripts\pip.exe" install -e ".[dev]"
  if errorlevel 1 (
    echo Install failed.
    pause
    exit /b 1
  )
)

if "%~1"=="" (
  echo.
  echo  ========================================
  echo   AETHER TERMINAL
  echo   login, pick Claude/Google/Grok/Kimi/Open Code
  echo   then agent REPL  (type a goal, or /help)
  echo  ========================================
  echo.
  "%AETHER_EXE%"
  pause
) else (
  "%AETHER_EXE%" %*
)

endlocal
