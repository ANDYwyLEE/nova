@echo off
REM Start every enabled camera from cameras.ini in the background.
cd /d %~dp0
if exist venv\Scripts\activate.bat (call venv\Scripts\activate.bat) else (call venv\bin\activate.bat)
python cams.py start %*
