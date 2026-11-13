# Installation

Nova needs Python 3.12 or newer and the packages in `requirements.txt`. Nothing else: the `av` package from PyPI ships its own FFmpeg, and the GPU paths use the driver that is already installed.

What a camera costs, measured on a 13-window wall (Ubuntu 26.04, two RTX 4090, `--opengl --accel cuda -f 30`): a live 1080p H.264 camera takes 4–11 % of one core (0.85 and 18 Mbit/s streams), about 375 MB of RAM and 430–460 MiB of VRAM; recording it adds about 250 MB and 500 MiB (the NVENC session); a window whose camera is unreachable idles at 220 MB without a GPU context. Software decoding of the 18 Mbit/s stream costs 40 % of a core. Thirteen windows together: 0.4 cores and 4 GB. The window is up within a second of launch, the first frame follows in 2–6 s.
- [What the PyPI FFmpeg can do](#what-the-pypi-ffmpeg-can-do)
- [What has run on real hardware](#what-has-run-on-real-hardware)
- [Linux](#linux): Ubuntu, Debian, Mint, Fedora, RHEL/Rocky/Alma, openSUSE, Arch, arm64, WSL2
- [Windows](#windows)
- [macOS](#macos)
- [Updating](#updating)
- [Custom FFmpeg](custom-ffmpeg.md) (only for SRT or VAAPI)

## What the PyPI FFmpeg can do

Checked with `av` 19.0.1 on Linux (2026-10-11); the 19.0.2 wheel bundles the identical FFmpeg build (same libraries, codecs, formats and hardware devices; compared 2026-11-10). The wheel bundles its own FFmpeg (9.0.2), so nothing below depends on the distribution's ffmpeg package or on the Python version: `av` 19 ships one `cp312-abi3` wheel per platform that Python 3.12, 3.13 and 3.14 all use (there is no wheel for 3.10/3.11, so **Python 3.12 or newer** is required).

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

## What has run on real hardware

Every combination below was run with real cameras, most of them recording several at once; anything not listed has only been through the tests on GitHub's runners, which have no GPU.

| system | GPU | install | decoding (`--accel`) | recording |
|---|---|---|---|---|
| Ubuntu 26.04 | NVIDIA RTX 4090 (drivers 595 and 615), RTX 4060 (driver 615) | wheel | `cuda` | `h264_nvenc`, `hevc_nvenc`, `av1_nvenc` |
| Ubuntu 26.04 | Intel UHD 770 (13th generation) | wheel + two packages | `qsv` | `h264_qsv`, `hevc_qsv` |
| Ubuntu 26.04 | Intel UHD 770 | [custom FFmpeg](custom-ffmpeg.md) | `vaapi` | `h264_vaapi`, `hevc_vaapi` |
| Ubuntu 26.04 | AMD Radeon (Ryzen 9 9955HX, Mesa 26) | [custom FFmpeg](custom-ffmpeg.md) | `vaapi` | `h264_vaapi`, `hevc_vaapi` |
| Windows 11 | Intel UHD 620 (8th generation; drivers of 2020 and 2022) | Option A and Option B | `qsv`, `d3d11va`, `dxva2`, `d3d12va` | `h264_qsv`, `hevc_qsv` |
| Windows 11 | AMD Radeon (Ryzen 9 5900HX) | Option A and Option B | `d3d11va`, `dxva2`, `amf` | `h264_amf`, `h264_mf` |
| macOS 15 | Intel iMac 2019 (Radeon Pro 555X) | MacPorts | `videotoolbox` | `h264_videotoolbox` |
| any of them | none | wheel | software | `libx264` |
| Debian 11 | NVIDIA ION (2010, `nouveau`) on an Atom D525 | wheel, PySide6 6.9.3 and numpy 2.3.5 ([older systems](#linux)) | software, on up to four threads | `libx264` with `-preset ultrafast` |
| Debian 11 | the same card: `nouveau` with its firmware, and NVIDIA's driver 340.108 | [custom FFmpeg](custom-ffmpeg.md) ([an old NVIDIA card](#linux)) | `vaapi` and `vdpau` on `nouveau`, `vdpau` on NVIDIA's driver | `libx264` (the card has no encoder) |

Not run on hardware: NVIDIA on Windows, Apple Silicon outside CI, a Raspberry Pi or another ARM board (the arm64 install runs on GitHub's ARM runners, without a camera), AV1 encoding on an Intel or AMD GPU (the ones above have no AV1 encoder).

The Linux commands of the next section are also run as written, by an ordinary user, in a fresh image of every distribution named there (Python 3.12 to 3.14; Ubuntu and Debian on arm64 as well; Debian 12 and Ubuntu 22.04 with a Python from uv) and with Python 3.15 on Debian 13, each week: install, `nova doctor`, a recording through the launcher, a window on a virtual screen and the tests.

## Linux

Ubuntu 24.04 and 26.04, Debian 13, Linux Mint 22:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-dev build-essential portaudio19-dev \
    libgl1 libegl1 libglib2.0-0 libdbus-1-3 libfontconfig1 libxkbcommon-x11-0 libxcb-cursor0 \
    libxcb-icccm4 libxcb-keysyms1 libxcb-shape0

git clone https://github.com/ANDYwyLEE/nova.git && cd nova
python3 -m venv venv && . venv/bin/activate         # 24.04: Python 3.12, 26.04: 3.13/3.14 - same av wheel
pip install -U pip && pip install -r requirements.txt

python nova.py --help
cp cameras.example.ini cameras.ini && chmod 600 cameras.ini   # then edit it: see the user guide
./nova start                                # a window per enabled camera; ./nova -h lists the commands
```

Other distributions: the same steps, with these packages in place of the two `apt` lines.

- **Fedora**

  ```bash
  sudo dnf install -y git python3 python3-devel gcc portaudio-devel mesa-libGL mesa-libEGL \
      glib2 dbus-libs fontconfig libxkbcommon-x11 xcb-util-cursor xcb-util-wm xcb-util-keysyms
  ```
- **RHEL 10, Rocky Linux 10, AlmaLinux 10**: Fedora's packages, with EPEL for PortAudio's headers.

  ```bash
  sudo dnf install -y epel-release
  sudo dnf install -y git python3 python3-devel gcc portaudio-devel mesa-libGL mesa-libEGL \
      glib2 dbus-libs fontconfig libxkbcommon-x11 xcb-util-cursor xcb-util-wm xcb-util-keysyms
  ```
- **RHEL 9, Rocky Linux 9, AlmaLinux 9**: `python3` there is 3.9, so Python 3.12 by name, and `python3.12 -m venv venv` in place of `python3 -m venv venv`.

  ```bash
  sudo dnf install -y epel-release
  sudo dnf install -y git python3.12 python3.12-devel gcc portaudio-devel mesa-libGL mesa-libEGL \
      glib2 dbus-libs fontconfig libxkbcommon-x11 xcb-util-cursor xcb-util-wm xcb-util-keysyms
  ```
- **openSUSE Tumbleweed and Leap 16**

  ```bash
  sudo zypper install -y git python3 python3-devel gcc portaudio-devel Mesa-libGL1 Mesa-libEGL1 \
      libglib-2_0-0 libgthread-2_0-0 krb5 libdbus-1-3 libfontconfig1 libxkbcommon-x11-0 \
      libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0
  ```
- **Arch Linux** (Manjaro and EndeavourOS take the same packages)

  ```bash
  sudo pacman -Syu --needed git python base-devel portaudio libglvnd glib2 dbus fontconfig \
      libxkbcommon-x11 xcb-util-cursor xcb-util-wm xcb-util-keysyms
  ```
- **Which Python**: 3.12 or newer, whatever the distribution calls it. Ubuntu 24.04, Mint 22 and RHEL 10 have 3.12, Debian 13, openSUSE and Raspberry Pi OS 13 have 3.13, Ubuntu 26.04, Fedora and Arch have 3.14: all install the same `av` wheel. Python 3.15 works as well; `pip` compiles PyOpenGL-accelerate for it (no wheel yet), with the packages above. An older `python3` is the next point.
- **Debian 12, Ubuntu 22.04, Linux Mint 21** (their `python3` is 3.11 or 3.10): the `apt` lines above, and a newer Python from [uv](https://docs.astral.sh/uv/), which puts it into the home folder and leaves the system's Python alone:

  ```bash
  sudo apt install -y curl
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ~/.local/bin/uv venv --seed --python 3.13 venv && . venv/bin/activate     # in place of the `python3 -m venv venv` line
  ```

  Then `pip install …` and the rest as above. Run in `debian:12` and `ubuntu:22.04` images (November 2026); the weekly workflow repeats it. On Ubuntu 22.04 the deadsnakes PPA is another way: `sudo add-apt-repository ppa:deadsnakes/ppa`, then `python3.12 python3.12-venv python3.12-dev` in place of the three `python3…` packages, and `python3.12 -m venv venv`. With a venv of an older Python, `pip` answers `No matching distribution found for av>=19.0.2` (its line above that one lists `Requires-Python >=3.12`) and `./nova` says `the venv here was made with Python 3.11, and Nova needs Python 3.12 or newer`.
- **Older systems** (a C library before glibc 2.34: Debian 11, Ubuntu 20.04, RHEL 8): PySide6 6.12 has no wheel for them and `pip` ends with `No matching distribution found for PySide6==6.12.0`. PySide6 6.9.3 is the newest that installs there; with a Python from uv as in the point above (RHEL 8 has `python3.12` by name, and takes RHEL 9's package line):

  ```bash
  sed 's/==6\.12\.0/==6.9.3/' requirements.txt > requirements-old.txt
  pip install -r requirements-old.txt
  ```

  Run on a Debian 11 that had never been updated (an Atom D525 nettop of 2010 with NVIDIA ION graphics on the `nouveau` driver, OpenGL 3.3): the install, `nova doctor`, the tests, windows with and without `--opengl`, recording. The same lines were run once in `debian:11`, `ubuntu:20.04` and `rockylinux:8` images. Most of these systems are past their distribution's support, and so is this combination: it is what worked in November 2026, not something the weekly runs repeat.
- **A CPU without SSE4.2** (Intel before the Core i series, Atoms before 2013, AMD before the FX series; `grep -c sse4_2 /proc/cpuinfo` prints 0): the numpy wheels from 2.4 on are built for it and end with `Illegal instruction` on an older CPU, and every camera with them, with nothing in the log. `pip install "numpy<2.4"` after the requirements (2.3.5, for Python 3.11 to 3.14; run on the Atom D525 above). `nova doctor` says so (`CPU … no SSE4.2`, and a line under Checks), and the launcher names the signal a camera ended with. On such a CPU one thread may be too little for a camera's stream: [Performance tips](troubleshooting.md#performance-tips).
- **arm64** (Raspberry Pi 4 and 5 with a 64-bit system, other ARM boards and servers): `av` and PySide6 have aarch64 wheels, and the apt lines above are run on arm64 Ubuntu 24.04 and Debian 13 on GitHub's ARM runners, with the tests. The arm64 `av` wheel has no NVIDIA or Intel codecs; the hardware codecs in it are the V4L2 ones (`h264_v4l2m2m`, `hevc_v4l2m2m`, …, `--accel v4l2m2m`), which need a board whose kernel driver offers them. No Raspberry Pi has run Nova with cameras here, so that path is untested; 32-bit ARM systems have no wheels.
- PyAudio has no Linux wheel and is compiled during `pip install`: that is what `build-essential`, `python3-dev` and `portaudio19-dev` are for (without a compiler: `No such file or directory: 'x86_64-linux-gnu-gcc'`). The `lib…` packages are what Qt loads at run time and does not bring itself; a desktop has most of them already, a server, a container or WSL does not (`ImportError: libglib-2.0.so.0`, or `Could not load the Qt platform plugin "xcb"`). On openSUSE the same libraries are packaged one by one, hence `libgthread-2_0-0` and `krb5` (`ImportError: libgthread-2.0.so.0`, and `libgssapi_krb5.so.2` for Qt's network module, which the launcher's cameras need). `nova doctor` names a Qt module that does not load and the library it misses. Every package line here is run as written in a fresh image of its distribution by the [Install guide](../.github/workflows/install-guide.yml) workflow.
- **NVIDIA**: the proprietary driver is all that is required (`libnvcuvid.so`, `libnvidia-encode.so` come with it); the CUDA toolkit is not needed. Use `--accel cuda`. The wheel's NVENC needs **driver 570.0 or newer** (`The minimum required Nvidia driver for nvenc is 570.0` in the log otherwise): Ubuntu 26.04 ships that, on 24.04 pick `nvidia-driver-570` or newer rather than the 535/550 series. With an older driver Nova still runs and records, but encodes with libx264 on the CPU.
- **An old NVIDIA card** (GeForce 8 to 300 series, ION: NVIDIA's driver for them ended with the 340 series in 2019, and most systems run them on `nouveau`): `--accel cuda` is not for these cards. The decoder FFmpeg uses (NVDEC) is part of NVIDIA's current drivers, and Nova says so once and decodes on the CPU (`--accel=cuda cannot work here: the NVIDIA card runs on the nouveau driver …`). The card can still decode H.264, through VA-API or VDPAU, which the wheel does not have and the [custom build](custom-ffmpeg.md) does (with `libva-dev` and `libvdpau-dev` installed). Both ways were run on an NVIDIA ION (GT218) of 2010 under Debian 11:
  - **`nouveau`** needs the card's video firmware, which it may not ship. The nouveau project's [VideoAcceleration](https://nouveau.freedesktop.org/VideoAcceleration.html) page has the steps: their `extract_firmware.py` on NVIDIA's 325.15 driver file (nothing of it is installed), and the files it writes into `/lib/firmware/nouveau`; no reboot. `vainfo` then lists `VAProfileH264High`, and `--accel vaapi` or `--accel vdpau` decode. Without the firmware the log has `No support for codec h264 profile 77`.
  - **NVIDIA's 340.108 driver**, which no distribution of today packages (NVIDIA's installer, and kernel patches for anything newer than Linux 5.4): `--accel vdpau` decodes; there is no VA-API with it. Its `libGL.so.1` is from before libglvnd, which Nova's windows take into account.

  What it gives, for a 1080p camera at 10 fps in a window with `--opengl`, in percent of one Atom D525 core: software decoding 37 on `nouveau` and 30 on NVIDIA's driver; VDPAU on `nouveau` 22, VA-API on `nouveau` 27, VDPAU on NVIDIA's driver 51 (the pictures come back from the card to be drawn). So `--accel vaapi` on `nouveau` is the one worth having there, and software with `--opengl` is not far from it. The limits: a card of that age decodes one or two such cameras; one at 25 fps and 15 Mbit/s is beyond it (57 ms a picture), which Nova notices, says, and decodes on the CPU. FFmpeg's VDPAU refuses a full-range stream (`Unsupported sw format: yuvj420p`) and needs an X display, so not `nova record` over ssh; VA-API over ssh needs the user in the group `render`. There is no encoder on either driver: recordings go through `libx264`, and the log says so. `nova doctor` has a line for each of these cases.
- **Intel**: `--accel qsv` works with the wheel once Intel's driver and Quick Sync runtime are there (the wheel brings the dispatcher itself, `libvpl2` is not needed):

  | | |
  |---|---|
  | Ubuntu, Debian (non-free) | `sudo apt install intel-media-va-driver-non-free libmfx-gen1.2` |
  | Fedora | `sudo dnf install intel-media-driver intel-vpl-gpu-rt`, the driver from RPM Fusion's nonfree repository |
  | openSUSE | `sudo zypper install intel-media-driver libmfx-gen1_2` |
  | Arch | `sudo pacman -S intel-media-driver vpl-gpu-rt` |

  It has to be the driver build with the encoders: with Ubuntu's `intel-media-va-driver`, `h264_qsv` records only with `-low_power 1` and `hevc_qsv` not at all, and with Fedora's own `libva-intel-media-driver` neither opens. Ubuntu's line has run with cameras for hours; the others opened `h264_qsv` and `hevc_qsv` in an image of each distribution on the same GPU (UHD Graphics 770). `nova doctor` lists the Intel GPU, says whether Quick Sync starts and names the packages for the distribution it runs on. The runtime is Intel's for 11th-generation Core (Tiger Lake) and newer; an older Intel GPU needs `--accel vaapi`, as an AMD GPU does. An integrated Intel GPU next to NVIDIA cards can do the decoding and recording while an NVIDIA card draws the desktop: a camera then takes none of the NVIDIA cards' memory (run on an i9-13900K's UHD Graphics 770 with two RTX 4090, Ubuntu 26.04, November 2026; numbers in the changelog). `--gpu` and `NOVA_GPU_DEVICES` do not apply to Quick Sync (each camera says so once in its log). Which GPU draws the desktop is the system's business, not Nova's: Nova uses the OpenGL the session gives it.
- **AMD** (and Intel before the 11th generation): `--accel vaapi`, which the wheel's FFmpeg lacks: it needs the [custom build](custom-ffmpeg.md), and then decodes and records on the GPU through the VA driver that is part of Mesa. Run on a Ryzen 9 9955HX's Radeon graphics (Ubuntu 26.04, Mesa 26, November 2026): two 1080p cameras decoded and recorded with `h264_vaapi` at 2 to 3 % of a core and 240 MB each, where software decoding and libx264 took 35 to 59 % and twice the memory. Without `--gpu`, Nova takes the first GPU that is not an NVIDIA card, so the Radeon next to an NVIDIA card is found whichever of them the kernel numbered first.
- **Wayland**: Nova positions its frameless windows itself, which Wayland compositors do not allow, so on a Wayland session it runs under XWayland, even where the desktop exports `QT_QPA_PLATFORM=wayland` for every application. GNOME, KDE and Xfce's labwc honour X11 positions, so nothing needs setting up; on Ubuntu 26.04's GNOME, which has no X11 session any more, this is the normal way to run. sway and Hyprland ignore them, so there Nova places its windows through the compositor's IPC, without rules ([user guide, Layouts](user-guide.md#layouts)). `QT_QPA_PLATFORM = wayland` in `[env]` gives native Wayland windows, NVIDIA included. Desktops with an X11 session (Cinnamon, Xfce, KDE) need nothing.
- **WSL2** (Windows Subsystem for Linux): the Ubuntu lines as they are; windows go through WSLg, as XWayland windows. Run in a fresh Ubuntu 26.04 instance (WSL 2.6 on Windows 11, a laptop with Intel UHD Graphics 620, November 2026): install, `nova doctor`, a recording, a camera started and stopped through the launcher, windows with and without `--opengl`, the tests. Mesa drew there with its software renderer: `GALLIUM_DRIVER = d3d12` in the `[env]` section of `cameras.ini` makes it use the GPU through Direct3D (`nova doctor` then shows `D3D12 (Intel(R) UHD Graphics 620)` in place of `llvmpipe`, and says so when it finds the software renderer under WSL). There is no `/dev/dri` under WSL, so Quick Sync and VAAPI cannot decode there; NVIDIA under WSL was not tried. Nova on Windows itself ([below](#windows)) decodes on any GPU.
- **Headless servers** (recording/streaming only): `--headless` sets `QT_QPA_PLATFORM=offscreen`; no X server is needed. `nova start` in a session without a display (ssh, a service) is refused with a line that says so; `nova record` is the command there. NVIDIA decoding and NVENC work from any login. An Intel or AMD GPU is reached through `/dev/dri/renderD*`, which a desktop login is given and an ssh login or a service is not: put the user in the node's group (`sudo usermod -aG render $USER`, then log in again). Until then `--accel vaapi` reports `No VA display found for device /dev/dri/renderD…`, Quick Sync `Unknown error occurred`, and the camera decodes in software; `nova doctor` says when a node is closed to the user.

## Windows

### Option A: python.org CPython and PyPI wheels (recommended)

```bat
git clone https://github.com/ANDYwyLEE/nova.git
cd nova
py -3.13 -m venv venv
venv\Scripts\activate
pip install -U pip
pip install -r requirements.txt
python nova.py --help
copy cameras.example.ini cameras.ini
nova start
```

- **Use Python 3.13, not the newest.** PyAudio has Windows wheels for Python 3.8 to 3.13 and PyOpenGL-accelerate none for 3.15, so with a newer Python `pip` tries to compile them and stops at `Microsoft Visual C++ 14.0 or greater is required` (seen with Python 3.15.2, November 2026). A plain `py -m venv venv` takes the newest Python on the machine, and the Python install manager offers the newest first: get 3.13 from python.org, with `py install 3.13`, or with `winget install Python.Python.3.13`, and create the venv with `py -3.13` as above.
- Accel: `cuda` (NVIDIA), `qsv` (Intel: Quick Sync decodes and records, `h264_qsv`; run on a UHD 620 laptop, November 2026), `d3d11va` or `dxva2` (any GPU through the standard decoder; with them an AMD GPU records through `h264_amf`, an Intel one through `h264_qsv`), `amf` (AMD's own decoder). `d3d12va` falls back to software decoding where FFmpeg lacks the GPU's decode tier (an AMD Ryzen 5900HX, November 2026).
- `nova.bat` uses `venv\Scripts\python.exe` automatically, so `nova start` works from a plain command prompt without activating the venv.
- Intel Quick Sync needs nothing but Intel's graphics driver: run with the laptop maker's driver of 2020 and Intel's of 2022 on a UHD Graphics 620, with the same results. Without a desktop (ssh, a service) `qsv`, `d3d11va` and `d3d12va` still decode and Quick Sync and AMF still record; `dxva2` needs a desktop session.

### Option B: MSYS2 UCRT64

MSYS2's Python cannot use PyPI binary wheels, so `av` is compiled against MSYS2's own FFmpeg package (which ships NVENC, QSV, AMF and SRT):

```bash
pacman -Syu      # a fresh MSYS2 first: twice, the first run updates the core and closes the window
pacman -S --needed git make pkgconf \
    mingw-w64-ucrt-x86_64-{gcc,python-pip,python-wheel,cython,python-setuptools,python-numpy,python-requests,portaudio,pyside6,python-pyopengl,ffmpeg}
cd nova
python -m venv --system-site-packages venv && . venv/bin/activate
pip install xmltodict filelock PyOpenGL_accelerate
pip install --no-binary av av --no-cache-dir
```

Create the venv as `venv` itself: `./nova` runs `venv/bin/python`, and MSYS2's `ln -s` copies a directory (unless `MSYS=winsymlinks:nativestrict`), so a `venv` linked to another venv before the `pip install`s would lack everything installed after.

PyAudio's `setup.py` takes every Windows Python for one built with Microsoft's compiler and gives gcc its settings (`gcc: error: /MT: linker input file not found`). One line keeps them for those Pythons; MSYS2's gcc finds PortAudio in `/ucrt64` by itself:

```bash
pip download pyaudio --no-binary :all: --no-deps && tar xzf PyAudio-*.tar.gz && cd PyAudio-*/
sed -i "s/sys.platform == 'win32':/sys.platform == 'win32' and 'MSC' in sys.version:/" setup.py
pip install --no-build-isolation . && cd ..
```

## macOS

Minimum versions follow the `av` wheel: **Intel Macs need macOS 11 Big Sur or later** (`macosx_11_0_x86_64`), **Apple Silicon Macs need macOS 14 Sonoma or later** (`macosx_14_0_arm64`; every M-series Mac can run it). Python 3.12 or newer; the tests run with 3.15 on GitHub's Apple Silicon runner (`pip` compiles PyOpenGL-accelerate for it, with Xcode's tools). Older PyAV releases had no macOS wheels at all (before 12.0, March 2024) or shipped FFmpeg without VideoToolbox (12.0 to 12.2); since 12.3 the wheel has VideoToolbox, so a custom FFmpeg build is only needed for SRT.

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

Checked on Apple Silicon by the CI and, on 2026-11-10, on an Intel iMac (2019, macOS 15.7, MacPorts) with exactly these commands.

What VideoToolbox accelerates is decided by the Mac, not by the wheel: H.264 on every supported Mac, HEVC decode/encode on Apple Silicon and on Intel Macs from 2016 (6th-generation Core) on. On an older Intel Mac one direction may fall back to software while the other is accelerated; the log lines `HW decoder:` / `software decoder` and `[AVWriter]` show which.

`--opengl` requests the 3.3 core-profile context macOS requires; without it the QPainter renderer is used.

Every camera window is a process of its own, so the Dock shows one Nova icon per open window; their name there is Python, the program that runs them.

## Updating

```bash
cd nova && git pull
. venv/bin/activate && pip install -r requirements.txt
```

`requirements.txt` pins versions that are known to work together, except `av`: it names the oldest PyAV Nova supports (`av>=19.0.2`, the first release that hands output options to the protocol), and newer releases are welcome. `pip install -U av` takes the newest; `nova doctor` warns only about an older one. On a machine with a source-built `av` (MSYS2, [custom FFmpeg](custom-ffmpeg.md)), `pip install -U` replaces that build with the newest wheel: build the new release from source instead.
