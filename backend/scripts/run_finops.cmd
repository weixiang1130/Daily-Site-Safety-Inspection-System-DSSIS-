@echo off
rem Progress collector, single shot (SafetyOps-Finops, daily 07:15).
rem Inert until FINOPS_PWD is filled in .env.onprem by the operator.
chcp 65001 >nul
cd /d "%~dp0..\onprem"
set PYTHONIOENCODING=utf-8
python -m collectors.finops >> "%~dp0..\..\logs\finops.log" 2>&1
