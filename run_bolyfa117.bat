@echo off
REM BOLYFA 117 Data Logger Launcher for Windows 11
REM ================================================

echo BOLYFA 117 USB Digital Multimeter Data Logger
echo ================================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3 from https://python.org
    pause
    exit /b 1
)

REM Check if pyserial is installed
python -c "import serial" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing required package: pyserial...
    pip install pyserial
)

echo.
echo Choose mode:
echo   1. Live console output
echo   2. CSV data logger
echo   3. Web dashboard
echo   4. List available COM ports
echo.
set /p choice="Enter choice (1-4): "

if "%choice%"=="4" (
    python bolyfa117_logger.py --list
    pause
    exit /b 0
)

echo.
echo Enter COM port (e.g., COM3, COM4, or just 3, 4):
set /p port="COM port: "

REM Auto-fix: if user entered just a number, prepend "COM"
setlocal EnableDelayedExpansion
set "raw_port=%port%"
set "num_check="
for /f "delims=0123456789" %%a in ("%raw_port%") do set "num_check=%%a"
if "!num_check!"=="" (
    REM It's purely numeric, prepend COM
    set "port=COM%raw_port%"
    echo [INFO] Auto-corrected to %port%
)

if "%choice%"=="1" (
    python bolyfa117_logger.py --mode live --port %port%
) else if "%choice%"=="2" (
    python bolyfa117_logger.py --mode csv --port %port%
) else if "%choice%"=="3" (
    python bolyfa117_logger.py --mode dashboard --port %port%
) else (
    echo Invalid choice.
)

pause
