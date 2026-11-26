@echo off
setlocal
cd /d %~dp0
set PYEXE=
py -3 -c "exit()" >nul 2>&1 && set "PYEXE=py -3"
if "%PYEXE%"=="" python -c "exit()" >nul 2>&1 && set "PYEXE=python"
if "%PYEXE%"=="" (
    echo Python 3 not found. Run install.bat first.
    pause & exit /b 1
)
%PYEXE% app.py
pause
