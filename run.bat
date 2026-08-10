@echo off
setlocal
cd /d "%~dp0"

REM UTF-8 console. Without this the legacy code page cannot encode the UI
REM glyphs and Rich raises UnicodeEncodeError mid-command.
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
  echo [AETHER] Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Failed to create venv. Is Python installed?
    pause
    exit /b 1
  )
  echo [AETHER] Installing package...
  call .venv\Scripts\pip install -e ".[dev]"
  if errorlevel 1 (
    echo Install failed.
    pause
    exit /b 1
  )
)

call .venv\Scripts\activate.bat

if "%~1"=="" (
  echo.
  echo  ========================================
  echo   AETHER TERMINAL
  echo   login, pick Claude/Google/Grok/Kimi/Open Code
  echo   then agent REPL  (type a goal, or /help)
  echo  ========================================
  echo.
  aether
  pause
) else (
  aether %*
)

endlocal
