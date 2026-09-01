@echo off
rem SafetyOps war-room API server. Auto-restarts if it crashes.
rem Managed by Task Scheduler (SafetyOps-Server, at logon).
chcp 65001 >nul
cd /d "%~dp0..\backend\onprem"
set PYTHONIOENCODING=utf-8
set PUBLIC_DASHBOARD=true
:loop
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 >> "%~dp0..\logs\server.log" 2>&1
rem Wait before restart so a bind conflict does not spin the CPU.
timeout /t 10 /nobreak >nul
goto loop
