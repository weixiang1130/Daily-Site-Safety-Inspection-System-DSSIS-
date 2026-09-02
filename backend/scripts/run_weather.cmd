@echo off
rem Weather collector, single shot per trigger (SafetyOps-Weather, every 15 min).
chcp 65001 >nul
cd /d "%~dp0..\onprem"
set PYTHONIOENCODING=utf-8
python -m collectors.weather >> "%~dp0..\..\logs\weather.log" 2>&1
