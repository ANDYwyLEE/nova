#!/bin/sh

. ./venv/bin/activate

unset CFLAGS
unset LDFLAGS
unset PKG_CONFIG_PATH
unset PKG_CONFIG_LIBDIR
unset C_INCLUDE_PATH

export PYTHONPATH=$PYTHONPATH:.
export LD_LIBRARY_PATH=$HOME/deps/lib:$LD_LIBRARY_PATH
export DYLD_LIBRARY_PATH=$HOME/deps/lib:$DYLD_LIBRARY_PATH # for macOS
export PATH=$HOME/deps/bin:$PATH

#export NOVA_TIMEOUT=5000000 # 5 sec
export NOVA_TIMEOUT=10000000 # 10 sec
#export NOVA_AUDIO_DEVICE_INDEX=0

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
#export OPTS="--opengl --accel=cuda --fps=30 --border --log-level=ERROR"
#export OPTS="--opengl --accel=cuda -f 30 -b -l ERROR --ffmpeg '-resize 640x360"
#export OPTS="--opengl -f 30 -b -l ERROR"
#export OPTS="-f 30 -b -l DEBUG"

#export OPTS="--opengl --accel=cuda -f 30 -b -l CRITICAL"
export OPTS="--opengl --accel=cuda -l DEBUG"
#export OPTS="--opengl --accel=qsv -f 30 -b -l CRITICAL"
#export OPTS="--opengl --accel=videotoolbox -f 30 -b -l CRITICAL"

# ==============================================================================
# GPU Device Selection (NOVA_GPU_DEVICES)
# ==============================================================================
# Define target GPU indices (comma-separated).
# Used by: cuda, qsv, vaapi, d3d11va, dxva2
# Example: 0 (primary GPU only), 0,1 (randomly pick one for load balancing)
#export NOVA_GPU_DEVICES=0,1


#export NOVA_GPU_DEVICES=0

#Cavanon CAM
python3 nova.py $OPTS -i 'rtsp://192.168.1.22:554/user=admin&password=password&channel=1&stream=0' "$@" &

