@echo off
REM Stop the cameras started by startall.bat.
cd /d %~dp0
if exist venv\Scripts\activate.bat (call venv\Scripts\activate.bat) else (call venv\bin\activate.bat)
python cams.py stop %*
