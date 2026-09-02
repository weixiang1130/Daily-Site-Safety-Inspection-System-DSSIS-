@echo off
rem Headcount from the face-recognition device (SafetyOps-Face, every 5 min).
rem Aggregate counts only; never calls the person/face endpoints.
chcp 65001 >nul
cd /d "%~dp0..\onprem"
set PYTHONIOENCODING=utf-8
python -m collectors.face >> "%~dp0..\..\logs\face.log" 2>&1
