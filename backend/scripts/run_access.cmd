@echo off
rem Headcount collector, single shot (SafetyOps-Access, every 30 min).
rem Fails fast while firewall blocks 1433; starts working the moment it opens.
chcp 65001 >nul
cd /d "%~dp0..\onprem"
set PYTHONIOENCODING=utf-8
python -m collectors.access >> "%~dp0..\..\logs\access.log" 2>&1
