@echo off
setlocal
cd /d "%~dp0"

set "PY="
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PY=%~dp0.venv\Scripts\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\pythonw.exe" set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe" set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
if not defined PY set "PY=python"

start "" "%PY%" "%~dp0main.py"
endlocal