# s.ps1

if (Test-Path .\venv\Scripts\Activate.ps1) { . .\venv\Scripts\Activate.ps1 } else { . .\venv\bin\Activate.ps1 }

# export PYTHONPATH=$PYTHONPATH:.
$env:PYTHONPATH = "$($env:PYTHONPATH);."

$env:NOVA_TIMEOUT = "5000000"  # 5 sec
#$env:NOVA_AUDIO_DEVICE_INDEX = "0"


# ==============================================================================
# Hardware Acceleration Options (--accel)
# ==============================================================================
# (omit)        : Software Only (Default, highest stability)
# --accel=cuda  : NVIDIA GPUs (NVENC/CUVID)
# --accel=qsv   : Intel Quick Sync (Integrated/Arc Graphics)
# --accel=vaapi : Linux Universal (Intel/AMD)
# --accel=videotoolbox : macOS Native (Apple Silicon/Intel)
# --accel=d3d11va : Windows Modern (Direct3D 11)
# --accel=dxva2 : Windows Legacy (Old GPUs/Systems)
# ==============================================================================
# $OPTS = @("--opengl", "--accel=cuda", "--fps=30", "--border", "--log-level=DEBUG")
# $OPTS = @("--fps=30", "--border", "--log-level=DEBUG")
$OPTS = @("-f", "30", "-b", "-l", "DEBUG")

# ==============================================================================
# GPU Device Selection (NOVA_GPU_DEVICES)
# ==============================================================================
# Define target GPU indices (comma-separated).
# Used by: cuda, qsv, vaapi, d3d11va, dxva2
# Example: "0" (primary GPU only), "0,1" (randomly pick one for load balancing)
# ==============================================================================
#$env:NOVA_GPU_DEVICES = "0,1"


# python nova.py @OPTS -i "rtsp://rtsp:password@192.168.1.43:554/av_stream/ch0" @args
python nova.py @OPTS -i "rtsp://admin:password@192.168.1.45:554/stream_0" @args
# python nova.py @OPTS -i "rtsp://admin:password@192.168.1.26:554/av_stream/main" @args
