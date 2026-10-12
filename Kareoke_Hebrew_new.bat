@echo off
cd /d "%~dp0"
start "" "C:\Program Files\LM Studio\LM Studio.exe"
timeout /t 10 /nobreak >nul

echo Checking LM Studio server status...
:check_lm
curl -s http://localhost:1234/v1/models >nul 2>&1
if %errorlevel% == 0 (
    echo LM Studio server is running.
) else (
    echo.
    echo LM Studio server is NOT running.
    echo Please open LM Studio, go to the Local Server tab,
    echo select qwen3-4b, and press Start Server.
    echo Then press any key to continue...
    pause >nul
    goto check_lm
)

start python timing_editor.py
timeout /t 2 /nobreak >nul
start http://127.0.0.1:8765/