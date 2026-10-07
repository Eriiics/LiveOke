@echo off
cd /d "%~dp0"
.venv\Scripts\python -m vocalchain.restore_output
pause
