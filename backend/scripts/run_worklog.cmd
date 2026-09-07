@echo off
rem Worklog collector, single shot per trigger (SafetyOps-Worklog, every 15 min).
chcp 65001 >nul
cd /d "%~dp0..\onprem"
set PYTHONIOENCODING=utf-8
python -m collectors.worklog >> "%~dp0..\..\logs\worklog.log" 2>&1
