@echo off
title TradeCore Bulletproof Launcher
color 0A

echo ===================================================
echo   🚀 STARTING TRADECORE SYSTEM (v27.6)
echo ===================================================

:: 1. KILL ZOMBIE PROCESSES
echo [1/3] Cleaning up old processes...
taskkill /F /IM uvicorn.exe /T >nul 2>&1
taskkill /F /IM dart.exe /T >nul 2>&1
taskkill /F /IM python.exe /T >nul 2>&1
echo       Done.

:: 2. LAUNCH BACKEND (Intelligent Activation)
echo [2/3] Launching Backend Server...
start "TradeCore Backend" cmd /k "cd backend_quant_lab && if exist venv\Scripts\activate.bat (call venv\Scripts\activate.bat && echo [INFO] Using Local venv) else (call C:\Users\S3dwn\miniconda3\Scripts\activate.bat && echo [INFO] Using Conda Base) && python -m uvicorn main:app --reload"

:: Wait 5 seconds for backend to wake up
echo       Waiting for connection...
timeout /t 5 /nobreak >nul

:: 3. LAUNCH FRONTEND
echo [3/3] Launching Frontend Interface...
start "TradeCore Frontend" cmd /k "cd mobile_terminal && flutter run -d web-server"

echo.
echo ===================================================
echo   ✅ SYSTEM LAUNCHED
echo   - Check Telegram for 'System Startup' msg.
echo   - If Backend closes, run 'Step 4' below manually.
echo ===================================================
echo.
pause