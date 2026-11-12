@echo off
cd /d "%~dp0"
chcp 65001 >nul
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 server.py --demo --db instance/demo.sqlite3 --host 0.0.0.0
) else (
  python server.py --demo --db instance/demo.sqlite3 --host 0.0.0.0
)
pause
