@echo off

REM MSYS2 venv keeps its scripts in venv\bin; a native venv uses venv\Scripts
if exist venv\Scripts\activate.bat (call venv\Scripts\activate.bat) else (call venv\bin\activate.bat)

set PYTHONPATH=%PYTHONPATH%;.

REM set NOVA_TIMEOUT=5000000
set NOVA_TIMEOUT=10000000
REM set NOVA_AUDIO_DEVICE_INDEX=0


REM ==============================================================================
REM Hardware Acceleration Options (--accel)
REM ==============================================================================
REM (omit)        : Software Only (Default, highest stability)
REM --accel=cuda  : NVIDIA GPUs (NVENC/CUVID)
REM --accel=qsv   : Intel Quick Sync (Integrated/Arc Graphics)
REM --accel=vaapi : Linux Universal (Intel/AMD)
REM --accel=videotoolbox : macOS Native (Apple Silicon/Intel)
REM --accel=d3d11va : Windows Modern (Direct3D 11)
REM --accel=dxva2 : Windows Legacy (Old GPUs/Systems)
REM ==============================================================================
REM set OPTS=--opengl --accel=cuda --fps 30 --border --log-level=DEBUG
REM set OPTS=--fps 30 --border --log-level=DEBUG
set OPTS=--opengl --accel=qsv -f 30 -b -l DEBUG
REM set OPTS=-f 30 -b -l DEBUG

REM ==============================================================================
REM GPU Device Selection (NOVA_GPU_DEVICES)
REM ==============================================================================
REM Define target GPU indices (comma-separated).
REM Used by: cuda, qsv, vaapi, d3d11va, dxva2
REM Example: 0 (primary GPU only), 0,1 (randomly pick one for load balancing)
REM ==============================================================================
REM set NOVA_GPU_DEVICES=0,1



REM Vehicle Detection CAMERA
REM python nova.py %OPTS% -i "rtsp://admin:password@192.168.1.45:554/stream_0" %*


REM Restaurent Cam
python nova.py %OPTS% -i "rtsp://rtsp:password@192.168.1.43:554/av_stream/ch0" %*

REM Frontdesk Cam
REM python nova.py %OPTS% -i "rtsp://rtsp:password@192.168.1.41:554/av_stream/ch0" %*
