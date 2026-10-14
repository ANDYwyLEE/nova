# Installation

Nova needs Python 3.12 or newer and the packages in `requirements.txt`. Nothing else: the `av` package from PyPI ships its own FFmpeg, and the GPU paths use the driver that is already installed. `python-dvr` (two small pure-Python files, used only by `xmeye://`) is in `requirements.txt` as well.

- [What the PyPI FFmpeg can do](#what-the-pypi-ffmpeg-can-do)
- [Linux](#linux-ubuntu-2404-and-2604)
- [Windows](#windows)
- [macOS](#macos)
- [Updating](#updating)
- [Custom FFmpeg](custom-ffmpeg.md) (only for SRT or VAAPI)

## What the PyPI FFmpeg can do

Checked with `av` 19.0.1 on Linux (2026-10-11). The wheel bundles its own FFmpeg (9.0.2), so nothing below depends on the distribution's ffmpeg package or on the Python version: `av` 19 ships one `cp312-abi3` wheel per platform that Python 3.12, 3.13 and 3.14 all use (there is no wheel for 3.10/3.11, so **Python 3.12 or newer** is required).

| included in the wheel | not included |
|---|---|
| `h264_nvenc`, `hevc_nvenc`, `av1_nvenc`, `*_cuvid` (NVIDIA decode/encode) | SRT protocol (`srt://`) |
| `*_qsv` via libvpl (Intel), `*_amf` (AMD) | VAAPI (Intel/AMD on Linux without NVIDIA) |
| libx264, libx265, libsvtav1, libvpx, aac, opus | `drawtext` filter (no libfreetype) |
| rtmps / https / tls (gnutls) | anything newer than the wheel's FFmpeg |
| VideoToolbox on macOS: hwaccel decode plus `h264_videotoolbox`, `hevc_videotoolbox`, `prores_videotoolbox` encoders (in the wheel since `av` 12.3.0, July 2024; verified on CI) | |

Verify your own installation:

```bash
python -c "import av; from av.codec.hwaccel import hwdevices_available as h; print(h()); \
  print(sorted(n for n in av.codecs_available if any(k in n for k in ('nvenc','cuvid','qsv','amf','videotoolbox','vaapi'))))"
python -c "import av; av.open('srt://127.0.0.1:1', timeout=0.3)"    # 'Protocol not found' = no SRT
```

If you need SRT or VAAPI, build FFmpeg yourself: [custom-ffmpeg.md](custom-ffmpeg.md).

## Linux (Ubuntu 24.04 and 26.04)

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-dev portaudio19-dev \
    libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0

git clone https://github.com/ANDYwyLEE/nova.git && cd nova
python3 -m venv venv && . venv/bin/activate         # 24.04: Python 3.12, 26.04: 3.13/3.14 - same av wheel
pip install -U pip && pip install -r requirements.txt

python nova.py --help
cp cameras.example.ini cameras.ini && chmod 600 cameras.ini   # then edit it: see the user guide
./nova start                                # a window per enabled camera; ./nova -h lists the commands
```

- Ubuntu 24.04's `python3` is 3.12, which is enough: it installs the same `av` wheel, with the same NVIDIA/Intel/AMD codecs, as a newer Python. Ubuntu 22.04 (Python 3.10) needs a newer interpreter first (e.g. the deadsnakes PPA).
- `portaudio19-dev` is needed to build PyAudio; the Qt libraries cover the xcb platform plugin.
- **NVIDIA**: the proprietary driver is all that is required (`libnvcuvid.so`, `libnvidia-encode.so` come with it); the CUDA toolkit is not needed. Use `--accel cuda`. The wheel's NVENC needs **driver 570.0 or newer** (`The minimum required Nvidia driver for nvenc is 570.0` in the log otherwise): Ubuntu 26.04 ships that, on 24.04 pick `nvidia-driver-570` or newer rather than the 535/550 series. With an older driver Nova still runs and records, but encodes with libx264 on the CPU.
- **Intel**: `--accel qsv` needs `intel-media-va-driver-non-free` and the oneVPL runtime (`libvpl2`); untested on this project's machines. `--accel vaapi` needs the custom build.
- **Wayland**: Nova positions its frameless windows itself, which Wayland compositors do not allow, so on a Wayland session it runs under XWayland by default. GNOME and KDE honour X11 positions, so nothing needs setting up; on Ubuntu 26.04's GNOME, which has no X11 session any more, this is the normal way to run. sway and Hyprland tile every window: there import the rules from `nova layout export NAME sway|hyprland|hyprlang` ([user guide, Layouts](user-guide.md#layouts)). `QT_QPA_PLATFORM = wayland` in `[env]` gives native Wayland windows, NVIDIA included. Desktops with an X11 session (Cinnamon, Xfce, KDE) need nothing.
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

## macOS

Minimum versions follow the `av` wheel: **Intel Macs need macOS 11 Big Sur or later** (`macosx_11_0_x86_64`), **Apple Silicon Macs need macOS 14 Sonoma or later** (`macosx_14_0_arm64`; every M-series Mac can run it). Python 3.12 or newer. Older PyAV releases had no macOS wheels at all (before 12.0, March 2024) or shipped FFmpeg without VideoToolbox (12.0 to 12.2); since 12.3 the wheel has VideoToolbox, so a custom FFmpeg build is only needed for SRT.

```bash
xcode-select --install
sudo port install portaudio python312          # MacPorts (any python3xx >= 3.12)
git clone https://github.com/ANDYwyLEE/nova.git && cd nova
python3.12 -m venv venv && . venv/bin/activate
pip install -U pip
CFLAGS="-I/opt/local/include" LDFLAGS="-L/opt/local/lib" pip install -r requirements.txt   # PyAudio builds against MacPorts' PortAudio
python nova.py --opengl --accel videotoolbox -i rtsp://...
cp cameras.example.ini cameras.ini && chmod 600 cameras.ini && ./nova start   # ./nova -h lists the commands
```

What VideoToolbox accelerates is decided by the Mac, not by the wheel: H.264 on every supported Mac, HEVC decode/encode on Apple Silicon and on Intel Macs from 2016 (6th-generation Core) on. On an older Intel Mac one direction may fall back to software while the other is accelerated; the log lines `HW decoder:` / `software decoder` and `[AVWriter]` show which.

`--opengl` requests the 3.3 core-profile context macOS requires; without it the QPainter renderer is used.

## Updating

```bash
cd nova && git pull
. venv/bin/activate && pip install -r requirements.txt
```

`requirements.txt` pins versions that are known to work together. Avoid `pip install -U` without a package name: it upgrades `av` to whatever is newest, and on a machine with a source-built `av` it silently replaces that build with the wheel.
