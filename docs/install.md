# Installation

Nova needs Python 3.10 or newer and the packages in `requirements.txt`. Nothing else: the `av` package from PyPI ships its own FFmpeg, and the GPU paths use the driver that is already installed.

- [What the PyPI FFmpeg can do](#what-the-pypi-ffmpeg-can-do)
- [Linux](#linux-ubuntu-2404)
- [Windows](#windows)
- [macOS](#macos-117-big-sur-or-later)
- [Updating](#updating)
- [Custom FFmpeg](custom-ffmpeg.md) (only for SRT or VAAPI)

## What the PyPI FFmpeg can do

Checked with `av` 19.0.1 on Linux (2026-10-11):

| included in the wheel | not included |
|---|---|
| `h264_nvenc`, `hevc_nvenc`, `av1_nvenc`, `*_cuvid` (NVIDIA decode/encode) | SRT protocol (`srt://`) |
| `*_qsv` via libvpl (Intel), `*_amf` (AMD) | VAAPI (Intel/AMD on Linux without NVIDIA) |
| libx264, libx265, libsvtav1, libvpx, aac, opus | `drawtext` filter (no libfreetype) |
| rtmps / https / tls (gnutls) | anything newer than the wheel's FFmpeg |
| videotoolbox (macOS wheel; expected, verify below) | |

Verify your own installation:

```bash
python -c "import av; from av.codec.hwaccel import hwdevices_available as h; print(h()); \
  print(sorted(n for n in av.codecs_available if any(k in n for k in ('nvenc','cuvid','qsv','amf','videotoolbox','vaapi'))))"
python -c "import av; av.open('srt://127.0.0.1:1', timeout=0.3)"    # 'Protocol not found' = no SRT
```

If you need SRT or VAAPI, build FFmpeg yourself: [custom-ffmpeg.md](custom-ffmpeg.md).

## Linux (Ubuntu 24.04)

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-dev portaudio19-dev \
    libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0

git clone https://github.com/ANDYwyLEE/nova.git && cd nova
python3 -m venv python3.12 && ln -s python3.12 venv && . venv/bin/activate
pip install -U pip && pip install -r requirements.txt

python nova.py --help
cp cameras.example.ini cameras.ini          # then edit it: see the user guide
./nova start                                # a window per enabled camera; ./nova -h lists the commands
```

- `portaudio19-dev` is needed to build PyAudio; the Qt libraries cover the xcb platform plugin.
- **NVIDIA**: the proprietary driver is all that is required (`libnvcuvid.so`, `libnvidia-encode.so` come with it). The CUDA toolkit is not needed. Use `--accel cuda`.
- **Intel**: `--accel qsv` needs `intel-media-va-driver-non-free` and the oneVPL runtime (`libvpl2`); untested on this project's machines. `--accel vaapi` needs the custom build.
- **Wayland**: Nova positions its frameless windows itself, which Wayland compositors do not allow. Run it under XWayland: `QT_QPA_PLATFORM=xcb python nova.py …` (or put `QT_QPA_PLATFORM = xcb` in the `[env]` section of `cameras.ini`).
- **Headless servers** (recording/streaming only): `--headless` sets `QT_QPA_PLATFORM=offscreen`; no X server is needed.

## Windows

### Option A: python.org CPython and PyPI wheels (recommended)

```bat
git clone https://github.com/ANDYwyLEE/nova.git
cd nova
py -m venv venv
venv\Scripts\activate
pip install -U pip
pip install -r requirements.txt
python nova.py --help
copy cameras.example.ini cameras.ini
nova start
```

- PyAudio wheels exist on PyPI for Python 3.8 to 3.13, so no compiler is needed there. There is no Windows wheel for Python 3.14 yet (October 2026): use 3.13, or build PyAudio against PortAudio yourself.
- Accel: `cuda` (NVIDIA), `qsv` (Intel), `d3d11va` or `dxva2` (any GPU through the standard decoder), `amf` for AMD encoding.
- `nova.bat` uses `venv\Scripts\python.exe` automatically, so `nova start` works from a plain command prompt without activating the venv.

### Option B: MSYS2 UCRT64

MSYS2's Python cannot use PyPI binary wheels, so `av` is compiled against MSYS2's own FFmpeg package (which ships NVENC, QSV, AMF and SRT):

```bash
pacman -S --needed git make pkgconf \
    mingw-w64-ucrt-x86_64-{gcc,python-pip,python-wheel,python-cython,python-setuptools,python-numpy,python-requests,portaudio,pyside6,python-pyopengl,ffmpeg}
cd nova
python -m venv --system-site-packages python3.12 && ln -s python3.12 venv && . venv/bin/activate
pip install xmltodict filelock PyOpenGL_accelerate
pip install --no-binary av av --no-cache-dir
```

PyAudio assumes MSVC on Windows. Under MSYS2 build it by hand: `pip download pyaudio`, unpack, and in `setup.py`'s `win32` branch add `('MS_WIN64', '1')` to `defines`, `/ucrt64/include` to `include_dirs`, `/ucrt64/lib` to the library path and `portaudio` to `external_libraries`; then `python setup.py install`.

## macOS (11.7 Big Sur or later)

```bash
xcode-select --install
sudo port install portaudio python312          # MacPorts; or: brew install portaudio python@3.12
git clone https://github.com/ANDYwyLEE/nova.git && cd nova
python3.12 -m venv python3.12 && ln -s python3.12 venv && . venv/bin/activate
pip install -U pip
CFLAGS="-I/opt/local/include" LDFLAGS="-L/opt/local/lib" pip install -r requirements.txt   # Homebrew: /opt/homebrew
python nova.py --opengl --accel videotoolbox -i rtsp://...
cp cameras.example.ini cameras.ini && ./nova start      # several cameras; ./nova -h lists the commands
```

`--opengl` requests the 3.3 core-profile context macOS requires; without it the QPainter renderer is used.

## Updating

```bash
cd nova && git pull
. venv/bin/activate && pip install -r requirements.txt
```

`requirements.txt` pins versions that are known to work together. Avoid `pip install -U` without a package name: it upgrades `av` to whatever is newest, and on a machine with a source-built `av` it silently replaces that build with the wheel.
