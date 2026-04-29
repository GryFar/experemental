@echo off
chcp 65001 >nul
title Wiwang Poster Bot
setlocal

cd /d "%~dp0"

:: Activate venv if present
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)

:: Check python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.9+ and add to PATH.
    pause
    exit /b 1
)

echo Starting Wiwang Poster...
python wiwang_poster_loop.py
if errorlevel 1 (
    echo.
    echo [ERROR] Bot exited with error code %errorlevel%.
)
pause
