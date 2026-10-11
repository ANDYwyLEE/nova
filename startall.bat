@echo off

REM call python3.12\Scripts\activate.bat
REM call python3.12\bin\activate.bat
if exist venv\Scripts\activate.bat (call venv\Scripts\activate.bat) else (call venv\bin\activate.bat)

set PYTHONPATH=%PYTHONPATH%;.

set NOVA_TIMEOUT=5000000
REM NOVA_AUDIO_DEVICE_INDEX=0


REM ==============================================================================
REM Hardware Acceleration Options (--accel)
REM ==============================================================================
REM (omit)        : Software Only (Default, highest stability)
REM --accel=cuda  : NVIDIA GPUs (NVENC/CUVID)
REM --accel=qsv   : Intel Quick Sync (Integrated/Arc Graphics)
REM --accel=vaapi : Linux Universal (Intel/AMD)
REM --accel=d3d11va : Windows Modern (Direct3D 11)
REM --accel=dxva2 : Windows Legacy (Old GPUs/Systems)
REM --accel=videotoolbox : macOS Native (Apple Silicon/Intel)
REM ==============================================================================
REM set OPTS=--opengl --accel=cuda --fps=30 --border --log-level=DEBUG
REM set OPTS=--fps=30 --border --log-level=DEBUG
REM set OPTS=--opengl -f 30 -b -l ERROR
set OPTS=--opengl --accel=qsv -f 30 -b -l ERROR
REM
REM ==============================================================================
REM GPU Device Selection (NOVA_GPU_DEVICES)
REM ==============================================================================
REM Define target GPU indices (comma-separated).
REM Used by: cuda, qsv, vaapi, d3d11va, dxva2
REM Example: 0 (primary GPU only), 0,1 (randomly pick one for load balancing)
REM ==============================================================================
REM set NOVA_GPU_DEVICES=0,1



REM Vehicle Detection CAMERA
start "" /B python nova.py %OPTS% -i "rtsp://admin:password@192.168.1.45:554/stream_0" %*

REM Backyard etang CAM
start "" /B python nova.py %OPTS% -i "rtsp://192.168.1.24:554/user=andy&password=password&channel=1&stream=0" %*

REM Backyard etong mini CAM
REM start "" /B python nova.py %OPTS% -i "rtsp://admin:password@192.168.1.31:554/av_stream/main" %*

REM Cavanon CAM
REM start "" /B python nova.py %OPTS% -i "rtsp://192.168.1.22:554/user=andy&password=password&channel=1&stream=0" %*
start "" /B python nova.py %OPTS% -i "rtsp://192.168.1.22:554/user=admin&password=password&channel=1&stream=0" %*

REM Grand etang CAM
REM start "" /B python nova.py %OPTS% -i "rtsp://192.168.1.23:554/user=andy&password=password&channel=1&stream=0" %*

REM Restaurent Cam
start "" /B python nova.py %OPTS% -i "rtsp://rtsp:password@192.168.1.43:554/av_stream/ch0" %*

REM Frontdesk Cam
start "" /B python nova.py %OPTS% -i "rtsp://rtsp:password@192.168.1.41:554/av_stream/ch0" %*

REM Salon Cam
start "" /B python nova.py %OPTS% -i "rtsp://rtsp:password@192.168.1.44:554/av_stream/ch0" %*

REM Lounge Cam
start "" /B python nova.py %OPTS% -i "rtsp://rtsp:password@192.168.1.40:554/av_stream/ch0" %*

REM Office Cat CAM-S1
start "" /B python nova.py %OPTS% -i "rtsp://rtsp:password@192.168.1.38:554/av_stream/ch0" %*

REM Poulailler CAM1
start "" /B python nova.py %OPTS% -i "rtsp://admin:password@192.168.1.27:554/av_stream/main" %*

REM Poulailler CAM2
start "" /B python nova.py %OPTS% -i "rtsp://admin:password@192.168.1.28:554/av_stream/main" %*

REM Poulailler CAM3
start "" /B python nova.py %OPTS% -i "rtsp://admin:password@192.168.1.29:554/av_stream/main" %*

REM Poulailler CAM4
start "" /B python nova.py %OPTS% -i "rtsp://admin:password@192.168.1.30:554/av_stream/main" %*

REM Portable Duck CAM
start "" /B python nova.py %OPTS% -i "rtsp://admin:password@192.168.1.36:554/av_stream/main" %*

REM Atlier CAM
start "" /B python nova.py %OPTS% -i "rtsp://admin:password@192.168.1.26:554/av_stream/main" %*

