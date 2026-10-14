# Building FFmpeg yourself (only for SRT, VAAPI or CUDA filters)

The `av` wheel from PyPI bundles an FFmpeg (9.0.2 in av 19.0.1) with NVENC/NVDEC, Quick Sync (libvpl), AMF, x264/x265/SVT-AV1, libopus and TLS. Build your own only if you need something it lacks:

- the **SRT** protocol (`srt://` input or `-t srt://…` output)
- **VAAPI** decoding on Intel/AMD Linux machines without an NVIDIA GPU
- CUDA filters (`scale_npp`, `scale_cuda`) or a newer FFmpeg than the wheel ships
- on MSYS2 Windows: a tweak the packaged FFmpeg does not have (e.g. `QSV_MAX_SURFACES`)

The recipes install into `~/deps`. A PyAV built against it needs `~/deps/lib` at run time: set it in the `[env]` section of `cameras.ini` (`LD_LIBRARY_PATH = /home/you/deps/lib`; `DYLD_LIBRARY_PATH` on macOS; `PATH` on Windows) and the launcher passes it to every camera process. Remove the line to fall back to the wheel.

## What changes for `--ffmpeg`

Nothing has to be edited. The options of the decoders and encoders are read from the PyAV build at run time (`ffparser.py`), so a build with `h264_vaapi` routes `-rc_mode`, `-low_power`, … to it, and a newer nvenc's options are known as soon as the build is. The demuxer, muxer, input-protocol and `ffmpeg`-program tables in `ffopts_tables.py` are generated from FFmpeg 9.0.2; regenerate them when your build's major version differs (the SRT protocol's options, say, are already there):

```bash
python tools/gen_ffopts.py --ffmpeg ~/deps/bin/ffmpeg --source ~/deps/ffmpeg-9.0.2 > ffopts_tables.py
pytest tests/test_ffparser.py          # the table/build comparison skips when the versions differ
```

Output protocol options (SRT `latency`, `passphrase`, …) still go in the URL query: PyAV opens outputs without options whatever the build ([ffmpeg-options.md](ffmpeg-options.md)).

## Lessons learned

- A source-built `av` links against the **system** libx264/libx265/libsrt at build time. A later OS upgrade (libx264.so.164 → .165) silently breaks both `~/deps/bin/ffmpeg` and the `av` module. Rebuild after distro upgrades, or link those libraries statically.
- Pin `av` in `requirements.txt`; a blanket `pip install -U` replaces the source build with the wheel.
- Always verify with `ffmpeg -hwaccels`, `ffmpeg -decoders | grep -E "cuvid|vaapi|qsv"` and `ffmpeg -protocols | grep srt` before building `av`.
- PyAV's own installation page names FFmpeg 8.x for source builds; av 19.0.1 builds and runs against 9.0.2 as well (the version its wheel bundles), checked below.

## Linux (Ubuntu 26.04): NVIDIA + SRT + VAAPI

Checked on 2026-10-14 (Ubuntu 26.04.1, driver 595, two RTX 4090): FFmpeg 9.0.2 configures and builds in about two minutes on 32 cores, `pip install --no-binary av` against it in one; the result has the cuvid/nvenc/vaapi codecs, `hwdevices_available() == ['vdpau', 'cuda', 'vaapi', 'drm']` and the SRT protocol.

```bash
sudo apt install -y git curl pkg-config build-essential nasm yasm libx264-dev libx265-dev \
    libva-dev libdrm-dev libnuma-dev libssl-dev libsrt-openssl-dev libopus-dev portaudio19-dev
# CUDA filters only: sudo apt install -y nvidia-cuda-toolkit   (nvcc; add the three cuda lines below)

mkdir -p ~/deps && cd ~/deps
git clone --depth 1 https://git.videolan.org/git/ffmpeg/nv-codec-headers.git      # NVENC/NVDEC headers (header-only)
make -C nv-codec-headers install PREFIX=$HOME/deps
export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig

curl -LO https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz && tar xJf ffmpeg-9.0.2.tar.xz && cd ffmpeg-9.0.2
./configure --prefix=$HOME/deps --enable-shared --disable-static --enable-gpl --enable-nonfree \
    --enable-libx264 --enable-libx265 --enable-libopus --enable-openssl --enable-libsrt --enable-vaapi \
    --enable-nvenc --enable-nvdec --enable-cuvid --disable-doc --disable-ffplay
    # CUDA filters (scale_npp, scale_cuda), with nvidia-cuda-toolkit installed:
    #   --enable-cuda-nvcc --enable-libnpp --nvccflags="-gencode arch=compute_89,code=sm_89" \
    #   --extra-cflags="-I/usr/include" --extra-ldflags="-L/usr/lib/x86_64-linux-gnu"   # match arch to your GPU
make -j"$(nproc)" && make install

export PATH=$HOME/deps/bin:$PATH LD_LIBRARY_PATH=$HOME/deps/lib
ffmpeg -hide_banner -decoders | grep -E "cuvid|vaapi"; ffmpeg -protocols | grep srt

# av against it (pip fetches Cython and builds the extension; a few minutes)
cd ~/nova && . venv/bin/activate
PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig pip install --no-binary av av==19.0.1 --no-cache-dir
LD_LIBRARY_PATH=$HOME/deps/lib python -c "import av; print(av.library_versions)"
```

Without an NVIDIA GPU drop the `nv-codec-headers` lines and `--enable-nvenc --enable-nvdec --enable-cuvid`.

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
