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
title 大围重复网站检测
cd /d "%~dp0"

echo.
set "ISSUE="
set /p ISSUE=Input issue number, press Enter for auto latest: 

set "ARGS=--periods 10 --workers 8 --update-backup"
if not "%ISSUE%"=="" set "ARGS=--period %ISSUE% --periods 10 --workers 8 --update-backup"

echo.
echo 正在启动重复检测...
where py >nul 2>nul
if %errorlevel%==0 (
  %PY_CMD% detect_duplicate_sites.py %ARGS%
) else (
  %PY_CMD% detect_duplicate_sites.py %ARGS%
)
echo.
echo 运行结束，查看 大围杀号生肖数据统一归纳 里的 重复网站.txt
pause
