# Building FFmpeg yourself (only for SRT, VAAPI or CUDA filters)

The `av` wheel from PyPI bundles an FFmpeg (9.0.2 in av 19.0.1 and 19.0.2) with NVENC/NVDEC, Quick Sync (libvpl), AMF, x264/x265/SVT-AV1, libopus and TLS. Build your own only if you need something it lacks:

- the **SRT** protocol (`srt://` input or `-t srt://…` output)
- **VAAPI** on Linux, for decoding and recording on an AMD GPU or on an Intel GPU older than the 11th generation (a newer Intel GPU has Quick Sync through the wheel: [install.md](install.md#linux-ubuntu-2404-and-2604))
- CUDA filters (`scale_npp`, `scale_cuda`) or a newer FFmpeg than the wheel ships

Quick Sync is not a reason: the wheel has it on Linux and Windows, and so does MSYS2's FFmpeg package.

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
- `pip install -r requirements.txt` keeps a source build of 19.0.2 or newer (it asks for `av>=19.0.2`), but `pip install -U av` or a blanket `pip install -U` replaces it with the newest wheel. For a new PyAV release, run the `--no-binary av` command below again.
- Always verify with `ffmpeg -hwaccels`, `ffmpeg -decoders | grep -E "cuvid|vaapi|qsv"` and `ffmpeg -protocols | grep srt` before building `av`.
- PyAV's own installation page names FFmpeg 8.x for source builds; av 19.0.1 and 19.0.2 build and run against 9.0.2 as well (the version their wheels bundle): 19.0.1 checked below, 19.0.2 against MSYS2's FFmpeg 9.0.2.

## Linux (Ubuntu 26.04): NVIDIA + SRT + VAAPI

Checked on 2026-10-14 and again on 2026-11-12 (Ubuntu 26.04.1, NVIDIA driver 595, two RTX 4090 and the integrated Intel UHD 770): FFmpeg 9.0.2 configures and builds in under a minute on 32 cores, `pip install --no-binary av` against it in another; the result has the cuvid/nvenc/vaapi codecs, `hwdevices_available() == ['vdpau', 'cuda', 'vaapi', 'drm']` and the SRT protocol.

```bash
sudo apt install -y git curl pkg-config build-essential nasm yasm libx264-dev libx265-dev \
    libva-dev libdrm-dev libnuma-dev libssl-dev libsrt-openssl-dev libopus-dev portaudio19-dev
# CUDA filters only: sudo apt install -y nvidia-cuda-toolkit   (nvcc; add the three cuda lines below)

mkdir -p ~/deps && cd ~/deps
# NVENC/NVDEC headers (header-only). Take the release your driver supports, not the newest: n13.0.19.1 needs
# driver 570, n13.1.15.0 (and the main branch) driver 610. Each release's README names its minimum driver.
git clone --depth 1 --branch n13.0.19.1 https://git.videolan.org/git/ffmpeg/nv-codec-headers.git
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
PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig pip install --no-binary av av --no-cache-dir           # the newest release
LD_LIBRARY_PATH=$HOME/deps/lib python -c "import av; print(av.library_versions)"
```

Without an NVIDIA GPU drop the `nv-codec-headers` lines and `--enable-nvenc --enable-nvdec --enable-cuvid`.

What to expect from the result:

- **NVENC headers newer than the driver** build without a word and fail when a recording starts: `Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0` in the log at `-l DEBUG`, and the recording goes to `libx264` (a 1080p camera at 10 fps: 47 % of a core where NVENC takes 5 %). The log line `[AVWriter] libx264 …` is the sign; rebuild with the older headers.
- **VAAPI decodes and records.** `--accel vaapi` attaches the device to the standard decoder (`HW decoder: h264 + vaapi hwaccel`) and records with `h264_vaapi`; `-e hevc_vaapi` works the same way. An Intel GPU needs its VA driver for that (`intel-media-va-driver-non-free` was installed here). A codec the GPU has no encoder for is refused and named: `-e av1_vaapi does not open here (Function not implemented); using h264_vaapi` on a UHD 770. Without a rate option the encoder keeps the quality constant (FFmpeg notes `No quality level set; using default (20)`): 1.8 Mbit/s for an indoor 1080p camera at 10 fps; `-b:v 2M` gives 2.0.
- **Which GPU**: `--gpu N` (or the launcher's `NOVA_GPU_DEVICES`) selects `/dev/dri/renderD(128+N)` for VAAPI, decoder and encoder alike. On a machine with several GPUs that has to be the VAAPI one: here `renderD128` is the Intel GPU and `renderD129`, an NVIDIA card, answers `Failed to initialise VAAPI connection`, after which the camera decodes in software and records with the next encoder that opens. `nova doctor` lists which GPU is behind which node.
- Measured on the Intel GPU (UHD 770): a 1080p window with VAAPI decoding at 10 fps 2.4 % of a core, at 20 fps 5.8 %; decoding plus `h264_vaapi` recording 5 % and 200 MB per 10 fps camera, six cameras at once 42 % of a core.

## Windows (MSYS2 UCRT64)

Not needed for Quick Sync: the packaged FFmpeg of [Option B](install.md#option-b-msys2-ucrt64) has it (checked on an Intel UHD 620, November 2026). This is the recipe for a newer FFmpeg than MSYS2 packages; it builds with libvpl (Quick Sync) and without NVENC and AMF.

```bash
pacman -S --needed mingw-w64-ucrt-x86_64-{nasm,yasm,cmake,toolchain,x264,x265,onevpl}
pacman -Rdd mingw-w64-ucrt-x86_64-ffmpeg          # remove the packaged one first

mkdir -p ~/deps && cd ~/deps && git clone https://git.ffmpeg.org/ffmpeg.git && cd ffmpeg
export PKG_CONFIG_PATH=$HOME/deps/lib/pkgconfig:/ucrt64/lib/pkgconfig
export CFLAGS="-I$HOME/deps/include -I/ucrt64/include -DMS_WIN64"
export LDFLAGS="-L$HOME/deps/lib -L/ucrt64/lib -lportaudio"
./configure --prefix=$HOME/deps --enable-shared --disable-static --enable-gpl --enable-nonfree \
    --enable-libx264 --enable-libx265 --enable-libvpl --disable-vulkan
make -j"$(nproc)" && make install
export PATH=$HOME/deps/bin:$PATH

# let Python find the DLLs in ~/deps/bin (the venv of the install guide, Option B)
cd ~/nova && . venv/bin/activate
cat > "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/sitecustomize.py" << 'PY'
import os
home = os.getenv('HOME')
if home:
    os.add_dll_directory(os.path.join(home, 'deps', 'bin'))
PY
pip install --no-binary av av --no-cache-dir
```

PyAudio on MSYS2 is built by hand, with the MSVC-only parts of its `setup.py` undone: see [Installation, Option B](install.md#option-b-msys2-ucrt64).

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
