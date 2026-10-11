Nova - per-camera RTSP viewer / recorder / live streamer
=========================================================

One process per camera, one frameless window per process.  PySide6 + PyAV + PyOpenGL + PyAudio.
Decodes with the GPU when asked (--accel), records 30-minute MP4 segments, streams to YouTube/Twitch
(RTMP) or any HLS/SRT/mpegts target, plays the camera audio, takes snapshots.

CONTENTS
  1. Which FFmpeg do I need?  (PyPI wheel vs. custom build)
  2. Linux (Ubuntu 24.04)
  3. Windows (MSYS2 UCRT64 or native CPython)
  4. macOS (>= 11.7 Big Sur)
  5. Custom FFmpeg build (~/deps) - only for SRT / VAAPI / CUDA filters
  6. Running Nova: scripts, options, environment variables, keys
  7. Changes 2026-10-11


=========================================================
1. WHICH FFMPEG DO I NEED?
=========================================================

`pip install av` gives you a PyAV wheel that bundles its own FFmpeg (site-packages/av.libs).
Nova does not need anything else.  Checked on Linux with av 19.0.1 (2026-10-11):

    included in the wheel                          | NOT included
    -----------------------------------------------+------------------------------------------
    h264/hevc/av1 _nvenc, *_cuvid (NVIDIA)         | SRT protocol (srt://)
    *_qsv via libvpl (Intel)                       | VAAPI (Intel/AMD on Linux without NVIDIA)
    *_amf (AMD, Windows/Linux)                     | CUDA filters (scale_npp, scale_cuda) - unused by Nova
    libx264, libx265, libsvtav1, libvpx, aac, opus | anything newer than the wheel's FFmpeg
    rtmps / https / tls (gnutls)                   |
    videotoolbox (macOS wheel - expected, verify)  |

Rule of thumb:
  * NVIDIA (or Intel QSV / AMD AMF) + RTSP cameras + YouTube RTMP/HLS  ->  the wheel is enough.
  * You need srt:// input/output, or VAAPI on an Intel/AMD-only Linux box  ->  section 5.

Check what your installed av can do:

    python -c "import av; from av.codec.hwaccel import hwdevices_available as h; print(h()); \
      print(sorted(n for n in av.codecs_available if any(k in n for k in ('nvenc','cuvid','qsv','amf','videotoolbox','vaapi'))))"
    python -c "import av; av.open('srt://127.0.0.1:1', timeout=0.3)"   # 'Protocol not found' = no SRT

Do not run a blanket `pip install -U` on the venv: it replaces a source-built av with the wheel
(that is what happened on 2026-10-11).  requirements.txt pins the versions that are known to work.


=========================================================
2. LINUX (Ubuntu 24.04)
=========================================================

# system packages (PyAudio needs the PortAudio headers; the rest is for Qt/OpenGL)
sudo apt update
sudo apt install -y git build-essential python3 python3-venv python3-dev \
    portaudio19-dev libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0

# NVIDIA: the driver is enough for cuvid/nvenc (libnvcuvid.so, libnvidia-encode.so come with it).
#         The CUDA toolkit is NOT required for the wheel.
# Intel QSV: sudo apt install -y intel-media-va-driver-non-free libvpl2   (untested here)
# Intel/AMD VAAPI: needs the custom build, section 5.

mkdir -p ~/nova && cd ~/nova
python3 -m venv python3.14          # or python3.12 - any 3.10+
ln -s python3.14 venv
. venv/bin/activate
pip install -U pip
pip install -r requirements.txt     # av, PySide6, PyOpenGL(+accelerate), PyAudio, numpy, requests, xmltodict, filelock

# check and run
python nova.py --help
./s.sh                              # edit the URL inside first; --accel=cuda for NVIDIA

Accel values on Linux: cuda (NVIDIA), qsv (Intel, wheel), vaapi (Intel/AMD, custom build only), none.


=========================================================
3. WINDOWS
=========================================================

Option A - native CPython (python.org) + PyPI wheels (simplest)
---------------------------------------------------------------
  * Install Python 3.12+ from python.org, then in a venv:
        py -m venv venv
        venv\Scripts\activate
        pip install -U pip
        pip install -r requirements.txt
    PyAudio has Windows wheels on PyPI; if `pip install PyAudio` fails for a very new Python,
    use `pip install pipwin && pipwin install pyaudio`.
  * Scripts (s.bat / s.ps1 / startall.bat) look for venv\Scripts\activate first, then venv\bin.
  * Accel: cuda (NVIDIA), qsv (Intel), d3d11va / dxva2 (any GPU, through the standard decoder).
    Run the capability check from section 1 to see what the Windows wheel contains.

Option B - MSYS2 UCRT64 (what README used to describe)
------------------------------------------------------
https://www.msys2.org/
c:\msys64\msys2_shell.cmd -ucrt64 -defterm -here -no-start

pacman -S --needed --noconfirm \
    make git vim pkgconf diffutils base-devel \
    mingw-w64-ucrt-x86_64-gcc \
    mingw-w64-ucrt-x86_64-python-pip \
    mingw-w64-ucrt-x86_64-python-wheel \
    mingw-w64-ucrt-x86_64-python-cython \
    mingw-w64-ucrt-x86_64-python-setuptools \
    mingw-w64-ucrt-x86_64-python-numpy \
    mingw-w64-ucrt-x86_64-python-requests \
    mingw-w64-ucrt-x86_64-portaudio \
    mingw-w64-ucrt-x86_64-pyside6 \
    mingw-w64-ucrt-x86_64-python-pyopengl \
    mingw-w64-ucrt-x86_64-ffmpeg

  * MSYS2's Python cannot use PyPI binary wheels, so av must be compiled against MSYS2's own
    FFmpeg package (it ships nvenc/qsv/amf/srt - confirm with `ffmpeg -hwaccels`, `ffmpeg -protocols`):
        python -m venv --system-site-packages python3.12
        ln -s python3.12 venv
        . venv/bin/activate
        pip install xmltodict filelock PyOpenGL_accelerate
        pip install --no-binary av av --no-cache-dir
  * PyAudio assumes MSVC on Windows; under MSYS2 build it by hand:
        pip download pyaudio && tar -xvf PyAudio-*.tar.gz && cd PyAudio-0.2.14
        # in setup.py, win32 branch:
        #     defines.append(('MS_WIN64', '1')); include_dirs += ['/ucrt64/include']
        #     external_libraries_path = ['/ucrt64/lib']; external_libraries.append('portaudio')
        python setup.py install
  * Only if MSYS2's ffmpeg lacks something you need (e.g. QSV_MAX_SURFACES tuning): section 5.

Accel values on Windows: cuda, qsv, d3d11va, dxva2, d3d12va, amf (encode).


=========================================================
4. macOS (>= 11.7 Big Sur)
=========================================================

xcode-select --install
# MacPorts (https://www.macports.org/install.php) - or Homebrew, your choice
sudo port selfupdate
sudo port install portaudio python312

mkdir -p ~/nova && cd ~/nova
python3.12 -m venv python3.12
ln -s python3.12 venv
. venv/bin/activate
pip install -U pip
export CFLAGS="-I/opt/local/include" LDFLAGS="-L/opt/local/lib"    # for PyAudio (PortAudio headers)
pip install -r requirements.txt

  * The macOS av wheel is expected to include videotoolbox; verify with the section-1 check.
  * Accel value: videotoolbox.  Run with --opengl (3.3 core profile is requested automatically).
  * s.sh / startall.sh work as-is (sh).


=========================================================
5. CUSTOM FFMPEG BUILD (~/deps) - ONLY FOR SRT / VAAPI / CUDA FILTERS
=========================================================

When you need srt://, VAAPI, scale_npp/scale_cuda, or a newer FFmpeg than the wheel ships, build
FFmpeg into ~/deps and compile av against it.  s.sh / startall.sh add ~/deps/lib and ~/deps/bin to
the environment automatically when ~/deps/lib exists.

Caveats learned the hard way:
  * A source-built av links to the *system* libx264/libx265/libsrt at build time.  A later OS upgrade
    (libx264.so.164 -> .165) silently breaks ~/deps/bin/ffmpeg and the av module.  Rebuild after
    distro upgrades, or link those libs statically.
  * Keep the venv's av pinned (`av==<version>`); `pip install -U` replaces it with the wheel.
  * Delete ~/deps when you stop using it, so the scripts fall back to the wheel cleanly.

--- Linux (NVIDIA + SRT + VAAPI) --------------------------------------------------------------
sudo apt install -y git unzip wget pkg-config build-essential nasm yasm cmake libtool \
    libx264-dev libx265-dev libva-dev libdrm-dev llvm-dev libnuma-dev libssl-dev libsrt-openssl-dev \
    portaudio19-dev
# NVIDIA only: sudo apt install -y nvidia-cuda-toolkit ; sudo ln -s /usr /usr/local/cuda
mkdir -p ~/deps && cd ~/deps
git clone https://git.videolan.org/git/ffmpeg/nv-codec-headers.git && (cd nv-codec-headers && make && make install PREFIX=$HOME/deps)
export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig
git clone https://git.ffmpeg.org/ffmpeg.git && cd ffmpeg
./configure --prefix=$HOME/deps --enable-shared --disable-static --enable-gpl --enable-nonfree \
    --enable-libx264 --enable-libx265 --enable-openssl --enable-libsrt --enable-vaapi \
    --enable-ffnvcodec --enable-cuda-nvcc --enable-libnpp \
    --extra-cflags="-I/usr/local/cuda/include -I$HOME/deps/include" \
    --extra-ldflags="-L/usr/local/cuda/lib64 -L$HOME/deps/lib" \
    --nvccflags="-gencode arch=compute_89,code=sm_89"      # match your GPU; drop the three cuda lines on non-NVIDIA
make -j$(nproc) && make install
export PATH=$HOME/deps/bin:$PATH LD_LIBRARY_PATH=$HOME/deps/lib
ffmpeg -hide_banner -decoders | grep -E "cuvid|vaapi" ; ffmpeg -protocols | grep srt
# av against it
cd ~/nova && . venv/bin/activate
export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig C_INCLUDE_PATH=$HOME/deps/include LDFLAGS="-L$HOME/deps/lib"
pip install --no-binary av av --no-cache-dir

--- Windows (MSYS2, QSV via libvpl) ---------------------------------------------------------------
pacman -S --needed mingw-w64-ucrt-x86_64-{nasm,yasm,cmake,toolchain,x264,x265,onevpl}
pacman -Rdd mingw-w64-ucrt-x86_64-ffmpeg          # remove the packaged one first
mkdir -p ~/deps && cd ~/deps && git clone https://git.ffmpeg.org/ffmpeg.git && cd ffmpeg
export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig:/ucrt64/lib/pkgconfig
export CFLAGS="-I$HOME/deps/include -I/ucrt64/include -DMS_WIN64" LDFLAGS="-L$HOME/deps/lib -L/ucrt64/lib -lportaudio"
./configure --prefix=$HOME/deps --enable-shared --disable-static --enable-gpl --enable-nonfree \
    --enable-libx264 --enable-libx265 --enable-libvpl --disable-vulkan \
    --extra-cflags="-DQSV_MAX_SURFACES=64"
make -j$(nproc) && make install
export PATH=$HOME/deps/bin:$PATH
# let Python find ~/deps/bin DLLs
mkdir -p ~/nova/python3.12/lib/python3.12/site-packages
cat > ~/nova/python3.12/lib/python3.12/site-packages/sitecustomize.py << 'PY'
import os
home = os.getenv('HOME')
if home:
    os.add_dll_directory(os.path.join(home, 'deps', 'bin'))
PY
pip install --no-binary av av --no-cache-dir

--- macOS (videotoolbox + SRT) --------------------------------------------------------------------
sudo port install nasm yasm pkgconfig x264 x265 openssl srt
export PATH=$HOME/deps/bin:/opt/local/bin:/opt/local/sbin:$PATH
export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig:/opt/local/lib/pkgconfig CFLAGS="-I/opt/local/include" LDFLAGS="-L/opt/local/lib"
mkdir -p ~/deps && cd ~/deps && git clone https://git.ffmpeg.org/ffmpeg.git && cd ffmpeg
./configure --prefix=$HOME/deps --enable-shared --disable-static --enable-gpl --enable-nonfree \
    --enable-libx264 --enable-libx265 --enable-libsrt --enable-openssl \
    --enable-videotoolbox --enable-audiotoolbox --extra-ldflags="-Wl,-rpath,$HOME/deps/lib"
make -j$(sysctl -n hw.ncpu) && make install
cd ~/nova && . venv/bin/activate
export CFLAGS="-I$HOME/deps/include -I/opt/local/include" LDFLAGS="-L$HOME/deps/lib -L/opt/local/lib"
pip install --no-binary av av --no-cache-dir
pip install --no-binary pyaudio pyaudio --no-cache-dir


=========================================================
6. RUNNING NOVA
=========================================================

Scripts
  s.sh / s.bat / s.ps1     one camera (edit the URL and OPTS inside)
  startall.sh / .bat       all cameras, one process each, in the background
  stopall.sh / .bat        SIGINT to every nova.py (window geometry and zoom are saved on exit)

Typical options  (python nova.py --help for all)
  -i URL                 rtsp:// rtmp:// srt:// onvif://user:pw@ip[:port][/0|1]  xmeye://  hik://  dahua://
                         '0' = first capture device (/dev/video0, avfoundation 0), dshow://video=NAME, v4l2:///dev/video2
  --opengl               shader rendering, zero-copy NV12/YUV upload (recommended)
  --accel X              cuda | qsv | vaapi | videotoolbox | d3d11va | dxva2 | rkmpp | v4l2m2m ...
  -f 30 -b -g 20         display fps cap, coloured border, window grid size
  -r --record-interval 30m --recording-dir DIR      record (fragmented MP4 segments)
  -t rtmp://a.rtmp.youtube.com/live2/KEY            stream (H.264 is chosen automatically for RTMP)
  -t "https://a.upload.youtube.com/http_upload_hls?cid=KEY&copy=0&file=temp.m3u8" --ffmpeg "-f hls -hls_time 2 -method PUT"
  -e hevc_nvenc          force an encoder
  --ffmpeg "..."         raw FFmpeg options - see the table below for what actually takes effect
  -a                     start audio playback; -n NAME camera name for folders; --headless no window
  -l DEBUG               log level (TRACE VERBOSE DEBUG INFO WARNING ERROR)

--ffmpeg: what works (measured 2026-10-11 with libx264 and h264_nvenc; "n/a" = libav has no such thing)
  Nova is a library program: only options that are AVOptions of a codec, a (de)muxer or a protocol
  can take effect.  ffmpeg.exe-only options (-t -ss -map -re -vsync -y ...) are reported once and ignored.
  Unknown codec options are reported in the log:  "ignored unknown --ffmpeg options: {...}".

  input / decoder (applies to the camera connection and the decoder)
    -rtsp_transport udp|tcp      RTSP transport (default tcp)                   works
    -probesize N -analyzeduration N -fflags nobuffer -max_delay N -buffer_size N   works (format options)
    -timeout N / -rw_timeout N   socket timeouts in us (or NOVA_TIMEOUT)         works
    -threads N                   decoder threads (software decoders)             works
    -skip_frame nokey            decode only keyframes (software decoders)       works, cuvid ignores it
    -skip_loop_filter all        faster, slightly uglier h264 decoding           works
    -err_detect ignore_err  -flags low_delay                                    works
    -resize WxH                  decoder-side scaling, cuvid ONLY                works with --accel=cuda
    -surfaces N  -deint adaptive -drop_second_field 1                           cuvid only
    -lowres 1                    accepted but no effect for h264 (mjpeg/mpeg2 only)

  video encoder (recording and -t streaming)
    -c:v / -vcodec NAME          encoder (same as -e)                            works
    -b:v 4M  -maxrate 4M -bufsize 8M                                            works (both encoders)
    -crf 23  -preset ultrafast..veryslow  -tune zerolatency  -x264-params k=v   libx264 / libx265
    -rc vbr -cq 30  -preset p1..p7  -tune hq|ll|ull  -multipass -spatial-aq 1    nvenc
    -g N                         keyframe interval in frames (default 2 s)      works
    -bf 0  -profile:v baseline|main|high  -level 3.1  -refs N                    works
    -pix_fmt yuv444p             encoder pixel format (checked against encoder) works
    -s WxH                       output size (scaled before encoding)           works
    -r N                         output frame rate (frames dropped, never duplicated)   works
    -vf FILTERS                  libavfilter chain, e.g. transpose=1  hflip  crop=w:h:x:y,scale=640:-2
                                 works; drawtext needs libfreetype (not in the PyPI wheel)
    -vtag hvc1|hev1 / -tag:v    codec tag (HEVC in mp4/HLS defaults to hvc1)   works
    -flags +cgop  -threads N                                                    works
    NOTE: preset/tune names are encoder specific.  "-preset veryfast" makes nvenc fail and Nova
          falls back to libx264 (a WARN line tells you); use p1..p7 with nvenc.

  audio encoder
    -c:a / -acodec aac|libopus   encoder (opus not allowed in flv/mp4)          works
    -b:a 64k                     bitrate                                        works
    -ar 16000  -ac 1             sample rate / channels (camera audio is resampled)   works
    -an                          no audio track (also disables the silent track for live targets)

  container / muxer / output protocol
    -f mpegts|flv|mp4|matroska|hls                                              works
    -movflags +faststart         mp4 flags (default: +frag_keyframe+empty_moov) works
    -hls_time 2 -hls_list_size 4 -hls_flags ... -method PUT                     works (hls)
    -metadata title=Cam1         container metadata                             works
    -flvflags no_duration_filesize  -rtmp_live live -tcp_nodelay 1               works (rtmp)
    -latency 200000 -mode caller -passphrase ... -pbkeylen 32                   works (srt, custom build only)

  not possible through the library (ignored with a WARN line)
    -t -ss -to -y -map -re -vsync/-fps_mode -shortest -copyts -filter_complex -af -frames -stream_loop

Environment variables
  NOVA_TIMEOUT=10000000        socket / read timeout in microseconds (also bounds shutdown time)
  NOVA_GPU_DEVICES=0,1         GPU index(es) for cuda/qsv/vaapi; several = random pick per process
  NOVA_AUDIO_DEVICE_INDEX=N    force a PortAudio output device
  QT_QPA_PLATFORM=offscreen    set automatically for --headless

Keys (H / F1 shows this in the window)
  F/F11 fullscreen  R/F7 record  T/F8 stream  A/F5 audio  S snapshot  B border  H help  Esc reset zoom
  arrows move (Ctrl: grow, Alt: shrink)  PgUp/PgDn resize  Home 1:1  +/- or wheel zoom  P print URL
  mouse: drag move, edge drag resize, right-click fit window to video, middle-click fit video, double-click fullscreen

Output
  videos/<camera>/YYYYMMDD_HHMM_HHMM.mp4     fragmented MP4 (playable even after a crash), keyframe every 2 s
  snapshots/<camera>_YYYYMMDD_HHMMSS.png
  .uri_cache.json                            resolved onvif/xmeye URLs, 1 day, passwords not stored


=========================================================
7. CHANGES 2026-10-11 (code review follow-up)
=========================================================

  * Recording/streaming timestamps follow the camera's real timing (no A/V drift when the declared
    fps is wrong); outages become gaps instead of speeding the file up.
  * MP4 is written fragmented (+frag_keyframe+empty_moov).  Override: --ffmpeg "-movflags +faststart".
  * Encoder choice follows the output: RTMP -> H.264 only, HLS/mpegts -> H.264/HEVC, files -> the
    camera's codec family first.  (Before: av1_nvenc for everything, which YouTube RTMP rejects.)
  * Keyframe interval 2 s by default; HEVC in mp4/HLS tagged hvc1; live targets without camera audio
    get a silent AAC track (-an disables).
  * --ffmpeg understands stream specifiers (-b:v, -b:a, -c:v, -an, -vtag); muxer options reach the container.
  * --accel vaapi/videotoolbox/d3d11va/dxva2 are real hardware paths; software fallback is multi-threaded
    and logged honestly; unsupported --accel exits with a message.
  * onvif/xmeye/hik/dahua resolution runs on the decoder thread; cache stores no passwords; logs mask them.
  * Capture devices supported (-i 0, dshow://video=NAME, v4l2:///dev/videoN).
  * Geometry/zoom saved on Ctrl-C / stopall; --grid-size and -n work; multi-monitor layouts with a
    monitor left of / above the primary one work.
  * Windows scripts fixed (-i, argument passing); stopall.bat stops only Nova.
  * OpenGL: direct yuv420p upload for software decoding, OSD textures uploaded only on change, BT.709 for HD.
  * Robustness: recording thread errors are logged and stop recording cleanly; empty segments are deleted;
    blocking I/O is bounded by NOVA_TIMEOUT so exit never hangs.
  * orig_20261011.tar.gz holds the sources from before this change.
