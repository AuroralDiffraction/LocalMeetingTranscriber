@echo off
rem Meeting Transcriber launcher (Windows).
rem First start: creates .venv and installs everything (setup_check.py). Later starts: opens the app.
rem Run  "Start Meeting Transcriber.bat" debug  from a terminal to keep a console open and see errors.
setlocal
cd /d "%~dp0"
title Meeting Transcriber

set "VENV=.venv"
if not exist "%VENV%\Scripts\python.exe" if exist "venv\Scripts\python.exe" set "VENV=venv"
if not exist "%VENV%\Scripts\python.exe" (
    call :create_venv || goto :failed
)

"%VENV%\Scripts\python.exe" setup_check.py
if errorlevel 1 goto :failed

if /i "%~1"=="debug" (
    "%VENV%\Scripts\python.exe" m4a_joiner.py
    pause
    exit /b
)
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0m4a_joiner.py"
exit /b 0


:create_venv
echo First start - looking for Python 3.10 to 3.13 ...
set "PY="
for %%V in (3.12 3.13 3.11 3.10) do (
    if not defined PY (
        py -%%V -c "import tkinter" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys, tkinter; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo No suitable Python found. WhisperX needs Python 3.10 - 3.13.
    echo Install Python 3.12 from https://www.python.org/downloads/
    echo  - keep "tcl/tk and IDLE" and the "py launcher" ticked in the installer,
    echo then run this script again.
    exit /b 1
)
echo Creating the virtual environment with: %PY%
%PY% -m venv .venv || exit /b 1
set "VENV=.venv"
exit /b 0


:failed
echo.
echo Setup did not finish - see the messages above. Fix the problem and run this script again.
pause
exit /b 1
