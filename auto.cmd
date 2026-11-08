@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=init"
python -u "%~dp0site\scripts\auto_update.py" "%MODE%"
echo.
pause
