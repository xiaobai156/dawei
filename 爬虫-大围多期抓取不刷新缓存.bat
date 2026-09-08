@echo off
set "PY_CMD=py -3.11"
%PY_CMD% --version >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 is required. Please install it and add the py launcher to PATH.
  pause
  exit /b 1
)
chcp 65001 >nul
title 大围多期抓取-不刷新近10期缓存
cd /d "%~dp0"

echo.
echo Starting multi-issue scraping. recent_10_cache.json will not be updated.
%PY_CMD% -m dawei.cli.multi_issue --prompt-issues
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Done. Check each issue success/fail txt files and the all-failed summary.
pause
exit /b %EXIT_CODE%
