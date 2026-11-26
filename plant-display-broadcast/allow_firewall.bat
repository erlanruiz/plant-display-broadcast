@echo off
REM Run as Administrator once: lets TVs reach Plant Display through Windows Firewall
netsh advfirewall firewall add rule name="Plant Display 8090" dir=in action=allow protocol=TCP localport=8090
pause
