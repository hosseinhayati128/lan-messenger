@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python "%~dp0lan_messenger.py"
) else (
  py -3 "%~dp0lan_messenger.py"
)
pause
