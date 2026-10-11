@echo off

REM for /f "tokens=2" %%a in ('tasklist ^| find "python"') do  taskkill /PID %%a 
taskkill /IM python.exe /T
