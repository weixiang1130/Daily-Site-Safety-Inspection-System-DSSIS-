@echo off
rem Weather history archive for analysis, complete days only (SafetyOps-WeatherArchive, daily 10:00).
chcp 65001 >nul
cd /d "%~dp0..\onprem"
set PYTHONIOENCODING=utf-8
python -m collectors.weather_archive >> "%~dp0..\..\logs\weather_archive.log" 2>&1
