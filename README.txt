README.txt

===========================
LINUX Ubuntu 24.04

# custom compile Av with ffmpeg source
# For NVIDIA
# https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/ffmpeg-with-nvidia-gpu/index.html

sudo apt update


sudo apt install  -y \
    git unzip wget pkg-config build-essential \
    nasm yasm cmake \
    libtool libc6 libc6-dev \
    libx264-dev \
    libx265-dev \
    libva-dev \
    libdrm-dev \
    llvm-dev \
    libnuma1 \
    libnuma-dev \
    libavdevice-dev \
    libavfilter-dev \
    libavformat-dev \
    libavcodec-dev \
    libswscale-dev \
    libavutil-dev \
    libswresample-dev \
    portaudio19-dev \
    libssl-dev \
    libsrt-openssl-dev

TODO 
    - amf AMD GPU
    
# Nvidia GPU --accel=cuda
sudo apt install  -y nvidia-cuda-toolkit
which nvcc
  "/usr/bin/nvcc"
sudo ln -s /usr /usr/local/cuda


mkdir -p ~/deps
cd ~/deps
git clone https://git.videolan.org/git/ffmpeg/nv-codec-headers.git
cd nv-codec-headers
make
make install PREFIX=$HOME/deps

export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig


cd ~/deps
git clone https://git.ffmpeg.org/ffmpeg.git ffmpeg
cd ffmpeg
./configure --prefix=$HOME/deps \
    --enable-shared \
    --disable-static \
    --enable-gpl \
    --enable-nonfree \
    --enable-libx264 \
    --enable-libx265 \
    --enable-cuda-nvcc \
    --enable-libnpp \
    --enable-ffnvcodec \
    --enable-openssl \
    --enable-libsrt \
    --enable-decoder=h264_cuvid \
    --enable-encoder=h264_nvenc \
    --extra-cflags="-I/usr/local/cuda/include -I$HOME/deps/include" \
    --extra-ldflags="-L/usr/local/cuda/lib64 -L$HOME/deps/lib" \
    --nvccflags="-gencode arch=compute_89,code=sm_89"

make -j$(nproc)
make install

which -a ffmpeg
    nothing..
export PATH=$HOME/deps/bin:$PATH
export LD_LIBRARY_PATH=$HOME/deps/lib

which -a ffmpeg
    /home/andy/deps/bin/ffmpeg
ffmpeg -decoders - | grep cuvid

 V..... av1_cuvid            Nvidia CUVID AV1 decoder (codec av1)
 V..... h264_cuvid           Nvidia CUVID H264 decoder (codec h264)
 V..... hevc_cuvid           Nvidia CUVID HEVC decoder (codec hevc)
 V..... mjpeg_cuvid          Nvidia CUVID MJPEG decoder (codec mjpeg)
 V..... mpeg1_cuvid          Nvidia CUVID MPEG1VIDEO decoder (codec mpeg1video)
 V..... mpeg2_cuvid          Nvidia CUVID MPEG2VIDEO decoder (codec mpeg2video)
 V..... mpeg4_cuvid          Nvidia CUVID MPEG4 decoder (codec mpeg4)
 V..... vc1_cuvid            Nvidia CUVID VC1 decoder (codec vc1)
 V..... vp8_cuvid            Nvidia CUVID VP8 decoder (codec vp8)
 V..... vp9_cuvid            Nvidia CUVID VP9 decoder (codec vp9)


export C_INCLUDE_PATH="$HOME/deps/include"
export LDFLAGS="-L$HOME/deps/lib -L/usr/lib"

cd ~/nova
python3.12 -m venv python3.12
ln -s python3.12 venv
source venv/bin/activate
pip install -U pip
pip install wheel setuptools numpy
pip install xmltodict requests filelock
pip install --no-binary av av  --no-cache-dir
pip install PyAudio
pip install PySide6 
pip install PyOpenGL PyOpenGL_accelerate






===========================
Windows MSYS2 --accel=qsv

https://www.msys2.org/
c:\msys64\msys2_shell.cmd -ucrt64 -defterm -here -no-start

pacman -S --needed --noconfirm \
    make git vim pkgconf diffutils base-devel \
    mingw-w64-ucrt-x86_64-gcc \
    mingw-w64-ucrt-x86_64-nasm \
    mingw-w64-ucrt-x86_64-yasm \
    mingw-w64-ucrt-x86_64-cmake \
    mingw-w64-ucrt-x86_64-toolchain \
    mingw-w64-ucrt-x86_64-python-pip \
    mingw-w64-ucrt-x86_64-python-wheel \
    mingw-w64-ucrt-x86_64-python-cython \
    mingw-w64-ucrt-x86_64-python-setuptools \
    mingw-w64-ucrt-x86_64-python-numpy \
    mingw-w64-ucrt-x86_64-python-requests \
    mingw-w64-ucrt-x86_64-x264 \
    mingw-w64-ucrt-x86_64-x265 \
    mingw-w64-ucrt-x86_64-onevpl \
    mingw-w64-ucrt-x86_64-portaudio \
    mingw-w64-ucrt-x86_64-pyside6 \
    mingw-w64-ucrt-x86_64-python-pyopengl



PS: mingw-w64-ucrt-x86_64-shiboken6 **

which -a ffmpeg
pacman -Rdd mingw-w64-ucrt-x86_64-ffmpeg

mkdir -p ~/deps
cd ~/deps
git clone https://git.ffmpeg.org/ffmpeg.git ffmpeg
cd ffmpeg

export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig:/ucrt64/lib/pkgconfig
export CFLAGS="-I$HOME/deps/include -I/ucrt64/include -DMS_WIN64"
export LDFLAGS="-L$HOME/deps/lib -L/ucrt64/lib -lportaudio"

./configure --prefix=$HOME/deps \
    --enable-shared \
    --disable-static \
    --enable-gpl \
    --enable-nonfree \
    --enable-libx264 \
    --enable-libx265 \
    --enable-libvpl \
    --disable-vulkan \
    --enable-encoder=h264_qsv \
    --enable-decoder=h264_qsv \
    --extra-cflags="-DQSV_MAX_SURFACES=64"

make -j$(nproc)
make install

which -a ffmpeg
 ...nothing...

export PATH=$HOME/deps/bin:$PATH

which -a ffmpeg
/home/Andy/deps/bin/ffmpeg

ffmpeg -decoders | grep qsv
 V....D av1_qsv              AV1 video (Intel Quick Sync Video acceleration) (codec av1)
 V....D h264_qsv             H264 video (Intel Quick Sync Video acceleration) (codec h264)
 V....D hevc_qsv             HEVC video (Intel Quick Sync Video acceleration) (codec hevc)
 V....D mjpeg_qsv            MJPEG video (Intel Quick Sync Video acceleration) (codec mjpeg)
 V....D mpeg2_qsv            MPEG2VIDEO video (Intel Quick Sync Video acceleration) (codec mpeg2video)
 V....D vc1_qsv              VC1 video (Intel Quick Sync Video acceleration) (codec vc1)
 V....D vp8_qsv              VP8 video (Intel Quick Sync Video acceleration) (codec vp8)
 V....D vp9_qsv              VP9 video (Intel Quick Sync Video acceleration) (codec vp9)
 V....D vvc_qsv              VVC video (Intel Quick Sync Video acceleration) (codec vvc)

mkdir -p ~/nova
cd ~/nova
python3.12 -m venv --system-site-packages python3.12
ln -s python3.12 venv
source venv/bin/activate

#pip install -U pip
python3.12.exe -m pip install --upgrade pip
pip install xmltodict filelock
pip install PyOpenGL_accelerate

mkdir -p ~/nova/python3.12/lib/python3.12/site-packages
cat > ~/nova/python3.12/lib/python3.12/site-packages/sitecustomize.py << 'EOF'
import os
home = os.getenv('HOME')
if home:
    os.add_dll_directory(os.path.join(home, 'deps', 'bin'))
EOF

pip install --no-binary av av --no-cache-dir


cd ~/deps
pip download pyaudio
tar -xvf PyAudio-*.tar.gz
cd PyAudio-0.2.14
vim setup.py
    …
    elif sys.platform == 'win32':
        bits = platform.architecture()[0]
        if '64' in bits:
            defines.append(('MS_WIN64', '1'))
        include_dirs += ['/ucrt64/include']
        external_libraries_path = ['/ucrt64/lib']
        if "portaudio" not in external_libraries:
            external_libraries.append("portaudio")
   …
python3.12 setup.py install

Note: PyAudio for Windows assumes the MSVC compiler by default, so pip install fails in an MSYS2 (GCC) environment. Therefore, you need to modify the source code and install it manually.


===========================
macOS >= 11.7 Big Sur  --accel=videotoolbox

xcode-select --install

# uninstall HomeBrew if you want
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh)"

# download MacPorts and install
https://www.macports.org/install.php

unset PKG_CONFIG_LIBDIR
unset DYLD_LIBRARY_PATH
unset C_INCLUDE_PATH

# please check ~/.profile after install MacPorts & env | grep PATH
#export PATH=$HOME/deps/bin:$PATH
export PATH=$HOME/deps/bin:/opt/local/bin:/opt/local/sbin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/X11/bin

export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig:/opt/local/lib/pkgconfig
export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"

port version
sudo port selfupdate
sudo port install bash vim pkgconfig nasm yasm git x264 x265 portaudio python312 openssl srt

sudo port uninstall ffmpeg


mkdir -p ~/deps
cd ~/deps
git clone https://git.ffmpeg.org/ffmpeg.git ffmpeg
cd ffmpeg

./configure --prefix=$HOME/deps \
--enable-shared \
--disable-static \
--enable-gpl \
--enable-nonfree \
--enable-libx264 \
--enable-libx265 \
--enable-libsrt \
--enable-openssl \
--enable-videotoolbox \
--enable-audiotoolbox \
--extra-ldflags="-Wl,-rpath,$HOME/deps/lib"

make -j$(sysctl -n hw.ncpu)
make install

ffmpeg -hide_banner -hwaccels | grep videotool


export CFLAGS="-I$HOME/deps/include -I/opt/local/include"
export LDFLAGS="-L$HOME/deps/lib -L/opt/local/lib"

mkdir -p ~/nova
cd ~/nova
python3.12 -m venv python3.12
ln -s python3.12 venv
source venv/bin/activate
pip install -U pip
pip install wheel setuptools numpy
pip install xmltodict requests filelock
pip install --no-binary av av --no-cache-dir
pip install --no-binary pyaudio pyaudio --no-cache-dir
pip install PySide6
pip install PyOpenGL PyOpenGL_accelerate


===========================
andy@raptor:~/nova$ ls -al
drwxrwxr-x  2 andy andy  4096 Jan 16 03:51 .
drwxr-xr-x 31 andy andy  4096 Jan 16 15:28 ..
-rw-r--r--  1 andy andy  1209 Jan 16 02:17 audioplayer.py
-rw-r--r--  1 andy andy  7560 Jan 16 02:17 audioworker.py
-rw-r--r--  1 andy andy 18201 Jan 16 02:17 auth.py
-rw-r--r--  1 andy andy 12641 Jan 16 02:17 avwriter.py
-rw-r--r--  1 andy andy  1858 Jan 16 02:17 codec_names.py
-rw-r--r--  1 andy andy  9375 Jan 16 02:17 ffparser.py
-rw-r--r--  1 andy andy  1204 Jan 16 02:17 fonts.py
-rw-r--r--  1 andy andy  3226 Jan 16 02:17 keyhandler.py
-rw-r--r--  1 andy andy  3161 Jan 16 02:17 logsync.py
-rw-r--r--  1 andy andy  2571 Jan 16 02:17 mousehandler.py
-rw-r--r--  1 andy andy  2180 Jan 16 02:17 mytools.py
-rw-r--r--  1 andy andy  6906 Jan 16 04:04 novacore.py
-rw-r--r--  1 andy andy 19825 Jan 16 02:17 novagl.py
-rw-r--r--  1 andy andy 12268 Jan 16 13:44 nova.py
-rw-r--r--  1 andy andy   781 Jan 16 04:20 player.py
-rw-r--r--  1 andy andy 10655 Jan 16 23:52 README.txt
-rw-r--r--  1 andy andy  5970 Jan 16 04:09 recorder.py
-rw-r--r--  1 andy andy 96952 Dec 24 23:22 rec.png
-rw-r--r--  1 andy andy   810 Jan 17 06:57 recworker.py
-rw-r--r--  1 andy andy   841 Dec 25 13:44 requirements.txt
-rw-r--r--  1 andy andy  1988 Jan  8 23:05 s.bat
-rw-r--r--  1 andy andy 27803 Dec 24 23:22 snd.png
-rw-r--r--  1 andy andy  1888 Jan  8 23:05 s.ps1
-rwxr-xr-x  1 andy andy  8816 Jan 16 23:34 s.sh
-rw-r--r--  1 andy andy  3639 Jan  8 23:06 startall.bat
-rwxr-xr-x  1 andy andy  5525 Jan  9 00:38 startall.sh
-rw-r--r--  1 andy andy   124 Jan  5 08:52 stopall.bat
-rwxr-xr-x  1 andy andy  1129 Jan  9 00:38 stopall.sh
-rw-r--r--  1 andy andy  3020 Jan 16 03:58 streamer.py
-rw-rw-r--  1 andy andy 33463 Jan 16 03:50 strm.png
-rw-r--r--  1 andy andy  3348 Jan 16 02:17 util.py
-rw-r--r--  1 andy andy 21376 Jan 16 02:17 videoworker.py
-rw-r--r--  1 andy andy 20523 Jan 16 02:17 view.py
-rw-r--r--  1 andy andy  5767 Jan 16 02:17 zoomer.py

(python3.12) andy@raptor:~/nova$ vi s.sh
(python3.12) andy@raptor:~/nova$ ./s.sh
(python3.12) andy@raptor:~/nova$ vi startup.sh
(python3.12) andy@raptor:~/nova$ ./startall.sh
(python3.12) andy@raptor:~/nova$ vi stopall.sh
(python3.12) andy@raptor:~/nova$ ./stopall.sh
