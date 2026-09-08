@echo off
set "PY_CMD=py -3.11"
%PY_CMD% --version >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 is required. Please install it and add the py launcher to PATH.
  pause
  exit /b 1
)
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo Starting...
%PY_CMD% scrape_all_36.py --prompt-issue
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Done. Check current success/fail txt files.
pause
exit /b %EXIT_CODE%

