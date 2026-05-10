@echo off
REM Run sector model with the project virtualenv (has pandas, etc.)
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" main.py %*
exit /b %ERRORLEVEL%
