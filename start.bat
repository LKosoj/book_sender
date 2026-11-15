@echo off
rem BookSender launcher for Windows.
rem On first run it installs dependencies (setup.py), then starts the app.
setlocal
cd /d "%~dp0"
set "VENV=.venv\Scripts\python.exe"

rem --- auto-install if venv missing or dependencies incomplete ---
set "NEED_SETUP="
if not exist "%VENV%" set "NEED_SETUP=1"
if not defined NEED_SETUP (
    "%VENV%" -c "import PIL, pymupdf, tkinter" >nul 2>nul || set "NEED_SETUP=1"
)
if defined NEED_SETUP (
    echo First run: installing dependencies...
    set "BASE="
    py -3 -c "import sys" >nul 2>nul && set "BASE=py -3"
    if not defined BASE python -c "import sys" >nul 2>nul && set "BASE=python"
    if not defined BASE (
        echo Python not found. Install Python 3.9+ from https://python.org
        echo ^(enable "Add to PATH" and tcl/tk during setup^), then run this again.
        pause
        exit /b 1
    )
    %BASE% setup.py
    if errorlevel 1 (
        echo Setup failed.
        pause
        exit /b 1
    )
)

if not exist "%VENV%" (
    echo Could not create the .venv environment.
    pause
    exit /b 1
)

"%VENV%" book_sender.py %*
if errorlevel 1 pause
endlocal
