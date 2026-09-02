@echo off
rem Push the dashboard snapshot to the cloud wallboard
rem (SafetyOps-Wallboard, every 5 min). Site PCs read it with ?k=<WALL_TOKEN>.
rem Writes a Blob only - never triggers a redeploy (that would burn build minutes).
chcp 65001 >nul
cd /d "%~dp0..\onprem"
set PYTHONIOENCODING=utf-8
python -m collectors.push_wallboard >> "%~dp0..\..\logs\wallboard.log" 2>&1
