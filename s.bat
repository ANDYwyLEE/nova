@echo off
REM Run one camera in the foreground:  s.bat [ID] [nova.py args]
cd /d %~dp0
if exist venv\Scripts\activate.bat (call venv\Scripts\activate.bat) else (call venv\bin\activate.bat)
python cams.py run %*
