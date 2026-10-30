@echo off
rem Runs the daily level scanner (stocks, then crypto) and opens the dashboard.
cd /d "%~dp0"
python -I scan.py %*
if errorlevel 1 pause & exit /b 1
python -I scan.py --market crypto
if errorlevel 1 pause & exit /b 1
start "" "%~dp0output\latest.html"
