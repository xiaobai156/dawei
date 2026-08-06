@echo off
set "PY_CMD="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3"
if not defined PY_CMD (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
  echo Cannot find Python. Please install Python and add it to PATH.
  pause
  exit /b 1
)
cd /d "%~dp0"

echo.
set "ISSUE="
set /p ISSUE=Input required issue number: 
if "%ISSUE%"=="" (
  echo Issue number is required.
  pause
  exit /b 1
)

set "ARGS=--fixed-issue %ISSUE%"

echo.
echo Starting...
where py >nul 2>nul
if %errorlevel%==0 (
  %PY_CMD% scrape_all_36.py %ARGS%
) else (
  %PY_CMD% scrape_all_36.py %ARGS%
)

echo.
echo Done. Check current success/fail txt files.
pause

