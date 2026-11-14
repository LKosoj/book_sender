@echo off
rem BookSender launcher for Windows. Prefers the project .venv (Pillow/PyMuPDF).
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" book_sender.py %*
    goto :end
)
py -3 -c "import tkinter" >nul 2>nul && (
    py -3 book_sender.py %*
    goto :end
)
python -c "import tkinter" >nul 2>nul && (
    python book_sender.py %*
    goto :end
)
echo Python with tkinter was not found.
echo Install Python from python.org (enable tcl/tk during setup),
echo then run: python setup.py
pause
exit /b 1

:end
if errorlevel 1 pause
endlocal
