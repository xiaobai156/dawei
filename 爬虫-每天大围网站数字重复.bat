@echo off
set "PY_CMD=py -3.11"
%PY_CMD% --version >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 is required. Please install it and add the py launcher to PATH.
  pause
  exit /b 1
)
chcp 65001 >nul
title 大围重复网站检测
cd /d "%~dp0"

echo.
echo 正在启动重复检测...
%PY_CMD% detect_duplicate_sites.py --prompt-period --periods 10 --workers 8 --use-backup
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo 运行结束，查看 大围杀号生肖数据统一归纳 里的 重复网站.txt
pause
exit /b %EXIT_CODE%
