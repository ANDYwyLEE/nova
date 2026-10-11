@echo off
REM Stop only Nova processes (never every python.exe on the machine).
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'nova\.py' } | ForEach-Object { Write-Host ('Stopping PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }"
