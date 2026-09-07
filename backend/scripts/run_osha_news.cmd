@echo off
rem OSHA news collector, single shot per trigger (SafetyOps-OshaNews, every 6 hours).
chcp 65001 >nul
cd /d "%~dp0..\onprem"
set PYTHONIOENCODING=utf-8
python -m collectors.osha_news >> "%~dp0..\..\logs\osha_news.log" 2>&1
