# Building FFmpeg yourself (only for SRT, VAAPI or CUDA filters)

The `av` wheel from PyPI bundles an FFmpeg with NVENC/NVDEC, Quick Sync (libvpl), AMF, x264/x265/SVT-AV1 and TLS. Build your own only if you need something it lacks:

- the **SRT** protocol (`srt://` input or `-t srt://…` output)
- **VAAPI** decoding on Intel/AMD Linux machines without an NVIDIA GPU
- CUDA filters (`scale_npp`, `scale_cuda`) or a newer FFmpeg than the wheel ships
- on MSYS2 Windows: a tweak the packaged FFmpeg does not have (e.g. `QSV_MAX_SURFACES`)

The recipes install into `~/deps`. Nova's `s.sh` / `startall.sh` add `~/deps/lib` and `~/deps/bin` to the environment automatically when `~/deps/lib` exists; delete `~/deps` to fall back to the wheel.

## Lessons learned

- A source-built `av` links against the **system** libx264/libx265/libsrt at build time. A later OS upgrade (libx264.so.164 → .165) silently breaks both `~/deps/bin/ffmpeg` and the `av` module. Rebuild after distro upgrades, or link those libraries statically.
- Pin `av` in `requirements.txt`; a blanket `pip install -U` replaces the source build with the wheel.
- Always verify with `ffmpeg -hwaccels`, `ffmpeg -decoders | grep -E "cuvid|vaapi|qsv"` and `ffmpeg -protocols | grep srt` before building `av`.

## Linux (Ubuntu 24.04): NVIDIA + SRT + VAAPI

```bash
sudo apt install -y git unzip wget pkg-config build-essential nasm yasm cmake libtool \
    libx264-dev libx265-dev libva-dev libdrm-dev llvm-dev libnuma-dev libssl-dev \
    libsrt-openssl-dev portaudio19-dev
# NVIDIA only (CUDA filters): sudo apt install -y nvidia-cuda-toolkit && sudo ln -s /usr /usr/local/cuda

mkdir -p ~/deps && cd ~/deps
git clone https://git.videolan.org/git/ffmpeg/nv-codec-headers.git
( cd nv-codec-headers && make && make install PREFIX=$HOME/deps )
export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig

git clone https://git.ffmpeg.org/ffmpeg.git && cd ffmpeg
./configure --prefix=$HOME/deps --enable-shared --disable-static --enable-gpl --enable-nonfree \
    --enable-libx264 --enable-libx265 --enable-openssl --enable-libsrt --enable-vaapi \
    --enable-ffnvcodec --enable-cuda-nvcc --enable-libnpp \
    --extra-cflags="-I/usr/local/cuda/include -I$HOME/deps/include" \
    --extra-ldflags="-L/usr/local/cuda/lib64 -L$HOME/deps/lib" \
    --nvccflags="-gencode arch=compute_89,code=sm_89"   # match your GPU; drop the cuda/npp lines without NVIDIA
make -j"$(nproc)" && make install

export PATH=$HOME/deps/bin:$PATH LD_LIBRARY_PATH=$HOME/deps/lib
ffmpeg -hide_banner -decoders | grep -E "cuvid|vaapi"; ffmpeg -protocols | grep srt

# av against it
cd ~/nova && . venv/bin/activate
export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig C_INCLUDE_PATH=$HOME/deps/include LDFLAGS="-L$HOME/deps/lib"
pip install --no-binary av av --no-cache-dir
```

## Windows (MSYS2 UCRT64): Quick Sync via libvpl

```bash
pacman -S --needed mingw-w64-ucrt-x86_64-{nasm,yasm,cmake,toolchain,x264,x265,onevpl}
pacman -Rdd mingw-w64-ucrt-x86_64-ffmpeg          # remove the packaged one first

mkdir -p ~/deps && cd ~/deps && git clone https://git.ffmpeg.org/ffmpeg.git && cd ffmpeg
export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig:/ucrt64/lib/pkgconfig
export CFLAGS="-I$HOME/deps/include -I/ucrt64/include -DMS_WIN64"
export LDFLAGS="-L$HOME/deps/lib -L/ucrt64/lib -lportaudio"
./configure --prefix=$HOME/deps --enable-shared --disable-static --enable-gpl --enable-nonfree \
    --enable-libx264 --enable-libx265 --enable-libvpl --disable-vulkan \
    --extra-cflags="-DQSV_MAX_SURFACES=64"
make -j"$(nproc)" && make install
export PATH=$HOME/deps/bin:$PATH

# let Python find the DLLs in ~/deps/bin
mkdir -p ~/nova/python3.12/lib/python3.12/site-packages
cat > ~/nova/python3.12/lib/python3.12/site-packages/sitecustomize.py << 'PY'
import os
home = os.getenv('HOME')
if home:
    os.add_dll_directory(os.path.join(home, 'deps', 'bin'))
PY
pip install --no-binary av av --no-cache-dir
```

PyAudio on MSYS2 must be built by hand: `pip download pyaudio`, unpack, and in `setup.py`'s `win32` branch add `('MS_WIN64', '1')` to `defines`, `/ucrt64/include` to `include_dirs`, `/ucrt64/lib` to the library path and `portaudio` to `external_libraries`; then `python setup.py install`.

## macOS: VideoToolbox + SRT

```bash
sudo port install nasm yasm pkgconfig x264 x265 openssl srt     # MacPorts; Homebrew works too
export PATH=$HOME/deps/bin:/opt/local/bin:/opt/local/sbin:$PATH
export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig:/opt/local/lib/pkgconfig
export CFLAGS="-I/opt/local/include" LDFLAGS="-L/opt/local/lib"

mkdir -p ~/deps && cd ~/deps && git clone https://git.ffmpeg.org/ffmpeg.git && cd ffmpeg
./configure --prefix=$HOME/deps --enable-shared --disable-static --enable-gpl --enable-nonfree \
    --enable-libx264 --enable-libx265 --enable-libsrt --enable-openssl \
    --enable-videotoolbox --enable-audiotoolbox --extra-ldflags="-Wl,-rpath,$HOME/deps/lib"
make -j"$(sysctl -n hw.ncpu)" && make install

cd ~/nova && . venv/bin/activate
export CFLAGS="-I$HOME/deps/include -I/opt/local/include" LDFLAGS="-L$HOME/deps/lib -L/opt/local/lib"
pip install --no-binary av av --no-cache-dir
pip install --no-binary pyaudio pyaudio --no-cache-dir
```
