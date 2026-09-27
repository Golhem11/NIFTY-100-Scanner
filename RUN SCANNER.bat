@echo off
setlocal
cd /d "%~dp0"

title NIFTY 100 Scanner - LIVE

echo ===============================================
echo NIFTY 100 - 5 Minute Signal Scanner
echo AUTO-START MODE
echo ===============================================
echo.
echo This CMD window must remain open while the scanner is running.
echo Closing this CMD window will stop the scanner and send a Telegram notification.
echo.

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv .venv 2>nul
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo ERROR: Python is not installed.
        pause
        exit /b 1
    )
    python -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Could not create the virtual environment.
    pause
    exit /b 1
)

echo Installing/checking required packages...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Package installation failed.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -c "import streamlit, pandas, requests, pyotp; from SmartApi import SmartConnect; from SmartApi.smartWebSocketV2 import SmartWebSocketV2; print('All dependencies OK - SmartApi is available')"
if errorlevel 1 (
    echo ERROR: Required dependencies did not install correctly.
    echo SmartApi is required for the Angel One connection.
    pause
    exit /b 1
)

echo.
echo Starting scanner automatically...
echo DO NOT CLOSE THIS CMD WINDOW unless you want to stop the scanner.
echo.
.venv\Scripts\python.exe -m streamlit run app.py

pause
