@echo off
rem BookSender XTC-converter setup wrapper (Windows).
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" setup.py --xtc
pause
