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


#export NOVA_ONVIF_XML_TRACE=1
#export NOVA_TIMEOUT=5000000 # 5 sec
export NOVA_TIMEOUT=10000000 # 10 sec


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
#export OPTS="--opengl --accel=cuda --fps=30 --border --log-level=DEBUG"
#export OPTS="--opengl --accel=cuda -f 30 -b -l DEBUG --ffmpeg '-resize 640x360"
#export OPTS="--opengl -f 30 -b -l DEBUG"
#export OPTS="-f 30 -b -l DEBUG"

export OPTS="--opengl --accel=cuda -f 30 -b -l DEBUG"
#export OPTS="--opengl -f 30 -b -l DEBUG"
#export OPTS="--accel=cuda -f 30 -b -l DEBUG"
#export OPTS="--opengl --accel=qsv -f 30 -b -l DEBUG"
#export OPTS="--opengl --accel=videotoolbox -f 30 -l DEBUG"

# ==============================================================================
# GPU Device Selection (NOVA_GPU_DEVICES)
# ==============================================================================
# Define target GPU indices (comma-separated). 
# Used by: cuda, qsv, vaapi, d3d11va, dxva2
# Example: 0 (primary GPU only), 0,1 (randomly pick one for load balancing)
#export NOVA_GPU_DEVICES=0
#export NOVA_GPU_DEVICES=1
#export NOVA_GPU_DEVICES=0,1

#python3 nova.py  $OPTS  "$@"

#python3 nova.py  $OPTS -i 'onvif://user1:password@192.168.200.77'  "$@"
#python3 nova.py  $OPTS -i 'onvif://user1:password@192.168.200.77/0'  "$@"
#python3 nova.py  $OPTS -i 'onvif://user1:password@192.168.200.77:80/0'  "$@"
#python3 nova.py  $OPTS -i 'rtsp://user1:password@192.168.200.77/av_stream/ch0'  "$@"
#python3 nova.py  $OPTS -i 'rtsp://user1:password@192.168.200.77:554/av_stream/ch0'  "$@"

#python3 nova.py $OPTS -i 'xmeye://admin:password@192.168.1.22' "$@"
#python3 nova.py $OPTS -i 'xmeye://admin:password@192.168.1.22/0' "$@"
#python3 nova.py $OPTS -i 'xmeye://admin:password@192.168.1.22:34567' "$@"
#python3 nova.py $OPTS -i 'xmeye://admin:password@192.168.1.22:34567/0' "$@"
#python3 nova.py $OPTS -i 'rtsp://192.168.1.22/user=admin&password=password&channel=1&stream=0.sdp' "$@"
#python3 nova.py $OPTS -i 'rtsp://192.168.1.22:554/user=admin&password=password&channel=1&stream=0.sdp' "$@"

##python3 nova.py $OPTS -i 'xmeye://andy:password@192.168.1.24' "$@"
##python3 nova.py $OPTS -i 'xmeye://andy:password@192.168.1.24/0' "$@"
##python3 nova.py $OPTS -i 'xmeye://andy:password@192.168.1.24:34567/0' "$@"
#python3 nova.py $OPTS -i 'rtsp://192.168.1.24:554/user=andy&password=password&channel=1&stream=0.sdp' "$@"

#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.26' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.26/0' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.26:8899/0' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.26:554/av_stream/main' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.26:554/user=admin_password=password_channel=1_stream=0.sdp?real_stream' "$@"


#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.27' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.27/0' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.27:8899/0' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.27:554/av_stream/main' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.27:554/user=admin_password=password_channel=1_stream=0.sdp?real_stream' "$@"

#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.28' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.28/0' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.28:8899/0' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.28:554/av_stream/main' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.28:554/user=admin_password=password_channel=1_stream=0.sdp?real_stream' "$@"


#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.29' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.29/0' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.29:8899/0' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.29/av_stream/main' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.29:554/av_stream/main' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.29:554/user=admin_password=password_channel=1_stream=0.sdp?real_stream' "$@"

#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.30' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.30/0' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.30:8899/0' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.30/av_stream/main' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.30:554/av_stream/main' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.30:554/user=admin_password=password_channel=1_stream=0.sdp?real_stream' "$@"

#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.36' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.36/0' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.36:8899/0' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.36/av_stream/main' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.36:554/av_stream/main' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.36/user=admin_password=password_channel=1_stream=0.sdp?real_stream' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.36:554/user=admin_password=password_channel=1_stream=0.sdp?real_stream' "$@"

#python3 nova.py  $OPTS -i 'onvif://rtsp:password@192.168.1.38'  "$@"
#python3 nova.py  $OPTS -i 'onvif://rtsp:password@192.168.1.38/0'  "$@"
#python3 nova.py  $OPTS -i 'onvif://rtsp:password@192.168.1.38:80/0'  "$@"
#python3 nova.py  $OPTS -i 'rtsp://rtsp:password@192.168.1.38/av_stream/ch0' "$@"
#python3 nova.py  $OPTS -i 'rtsp://rtsp:password@192.168.1.38:554/av_stream/ch0' "$@"

##Lounge Cam
#python3 nova.py $OPTS -i 'onvif://rtsp:password@192.168.1.40' "$@" 
#python3 nova.py $OPTS -i 'onvif://rtsp:password@192.168.1.40/0' "$@" 
#python3 nova.py $OPTS -i 'onvif://rtsp:password@192.168.1.40:1000/0' "$@" 
#python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.40/av_stream/ch0' "$@" 
#python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.40:554/av_stream/ch0' "$@" 
##Frontdesk Cam
#python3 nova.py $OPTS -i 'onvif://rtsp:password@192.168.1.41' "$@" 
#python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.41/av_stream/ch0' "$@" 
#python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.41:554/av_stream/ch0' "$@" 
#Parking Cam (mal-function!)
#python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.42/av_stream/ch0' "$@" 
#python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.42:554/av_stream/ch0' "$@" 
##Restaurent Cam
#python3 nova.py $OPTS -i 'onvif://rtsp:password@192.168.1.43' "$@" 
#python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.43/av_stream/ch0' "$@" 
#python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.43:554/av_stream/ch0' "$@" 
##Salon Cam
#python3 nova.py $OPTS -i 'onvif://rtsp:password@192.168.1.44' "$@" 
#python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.44/av_stream/ch0' "$@" 
#python3 nova.py $OPTS -i 'rtsp://rtsp:password@192.168.1.44:554/av_stream/ch0' "$@" 


#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.45' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.45/0' "$@"
#python3 nova.py $OPTS -i 'onvif://admin:password@192.168.1.45:8080/0' "$@"
python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.45/stream_0' "$@"
#python3 nova.py $OPTS -i 'rtsp://admin:password@192.168.1.45:554/stream_0' "$@"

#python3 nova.py $OPTS -i "rtmp://192.168.1.7:1935/live/mystream" "$@"
#python3 nova.py $OPTS -i "srt://192.168.1.7:1935?mode=caller" "$@"
#python3 nova.py $OPTS -i "srt://192.168.1.7:1935?mode=caller&passphrase=mysecretkey123&pbkeylen=32" "$@"

# to Youtube streaming
#./s.sh -t rtmp://a.rtmp.youtube.com/live2/YOUR-STREAM-KEY
#./s.sh -t "https://a.upload.youtube.com/http_upload_hls?cid=YOUR-STREAM-KEY&copy=0&file=temp.m3u8" -e hevc_nvenc --ffmpeg "-f hls -hls_time 2 -hls_list_size 4 -method PUT -vtag hvc1" --headless
