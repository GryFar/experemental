@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
python wiwang_poster_loop.py
pause
