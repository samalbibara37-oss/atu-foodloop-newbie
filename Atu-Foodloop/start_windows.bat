@echo off
cd /d "%~dp0"
echo FoodLoop ATU
echo 1 - Private database, your admin password (recommended)
echo 2 - Demonstration data and public demo passwords
echo.
set "FOODLOOP_MODE=1"
set /p "FOODLOOP_MODE=Choose 1 or 2 [1]: "
if "%FOODLOOP_MODE%"=="2" (
  call start_demo_windows.bat
) else (
  call start_private_windows.bat
)
