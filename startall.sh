#!/bin/sh

. ./venv/bin/activate

unset CFLAGS
unset LDFLAGS
unset PKG_CONFIG_PATH
unset PKG_CONFIG_LIBDIR
unset C_INCLUDE_PATH

export PYTHONPATH=$PYTHONPATH:.
# Only needed with a custom FFmpeg build in ~/deps (README section 6: SRT / VAAPI).
# The PyPI av wheel ships its own FFmpeg and ignores these.
if [ -d "$HOME/deps/lib" ]; then
    export LD_LIBRARY_PATH=$HOME/deps/lib:$LD_LIBRARY_PATH
    export DYLD_LIBRARY_PATH=$HOME/deps/lib:$DYLD_LIBRARY_PATH # macOS
    export PATH=$HOME/deps/bin:$PATH
fi

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

export OPTS="--opengl --accel=cuda -f 30 -b -l CRITICAL"
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

# Vehicle Detection CAMERA
python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.45:554/stream_0' "$@" &

#Backyard etang CAM
python3 nova.py $OPTS -i 'rtsp://192.168.1.24:554/user=andy&password=password&channel=1&stream=0' "$@" &

## Backyard etong mini CAM
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.31:554/av_stream/main' "$@" &

#Cavanon CAM
#python3 nova.py $OPTS -i 'rtsp://192.168.1.22:554/user=andy&password=password&channel=1&stream=0' "$@" &
python3 nova.py $OPTS -i 'rtsp://192.168.1.22:554/user=admin&password=password&channel=1&stream=0' "$@" &

##Grand etang CAM
#python3 nova.py $OPTS -i 'rtsp://192.168.1.23:554/user=andy&password=password&channel=1&stream=0' "$@" &


#export NOVA_GPU_DEVICES=1

#Restaurent Cam
python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.43:554/av_stream/ch0' "$@" &
#Frontdesk Cam
python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.41:554/av_stream/ch0' "$@" &
#Salon Cam
python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.44:554/av_stream/ch0' "$@" &
#Lounge Cam
python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.40:554/av_stream/ch0' "$@" &



#Office Cat CAM-S1
python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.38:554/av_stream/ch0' "$@" &

# Poulailler CAM1
python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.27:554/av_stream/main' "$@" &
# Poulailler CAM2
python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.28:554/av_stream/main' "$@" &
# Poulailler CAM3
python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.29:554/av_stream/main' "$@" &
# Poulailler CAM4
python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.30:554/av_stream/main' "$@" &
# Portable Duck CAM
python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.36:554/av_stream/main' "$@" &

# Atlier CAM
python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.26:554/av_stream/main' "$@" &

#Parking Cam (mal-function!)
#python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.42:554/av_stream/ch0' "$@" &

#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=1&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=2&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=3&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=4&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=5&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=6&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=7&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=8&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=9&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=10&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=11&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=12&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=13&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=14&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=15&stream=0' "$@" &
#python3 nova.py $OPTS -i 'rtsp://192.168.1.5:554/user=rtsp&password=password&channel=16&stream=0' "$@" &
