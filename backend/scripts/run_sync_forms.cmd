@echo off
rem Cloud form sync, single shot per trigger (SafetyOps-Sync, 07:00 and 19:00).
chcp 65001 >nul
cd /d "%~dp0..\onprem"
set PYTHONIOENCODING=utf-8
python -m collectors.sync_forms >> "%~dp0..\..\logs\sync_forms.log" 2>&1
