@echo off
rem Double-click to remove Host Load from this PC and from your BUSY Bar.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\install.ps1" -Uninstall
pause
