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
chcp 65001 >nul
title 大围多期抓取-不刷新近10期缓存
cd /d "%~dp0"

echo.
set "ISSUES="
set /p ISSUES=Input issue numbers, example: 187 188 189: 

set "HAS_ISSUE="
for %%I in (%ISSUES%) do set "HAS_ISSUE=1"
if not defined HAS_ISSUE (
  echo No issue numbers entered.
  pause
  exit /b 1
)

echo.
echo Starting multi-issue scraping. recent_10_cache.json will not be updated.
%PY_CMD% -m dawei.cli.multi_issue %ISSUES%
echo.
echo Done. Check each issue success/fail txt files and the all-failed summary.
pause
