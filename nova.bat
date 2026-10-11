@echo off
REM Nova launcher:  nova start | record | run | stop | status | list      (nova -h for help)
REM Cameras and their passwords live in cameras.ini (git-ignored); cameras.example.ini is the template.
REM Uses the project venv when there is one; cams.py does the work.
setlocal
set "PY=python"
if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"
"%PY%" "%~dp0cams.py" %*
