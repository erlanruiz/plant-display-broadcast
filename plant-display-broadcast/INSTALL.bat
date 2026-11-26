@echo off
REM Double-click this to run the Plant Display installer as administrator.
echo Launching the Plant Display installer (will request admin rights)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0install.ps1'"
