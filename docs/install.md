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
| Debian 13 | the same machine after two release upgrades: `nouveau` (Mesa 25) with its firmware | wheel with PySide6 6.9.3 and numpy 2.3.5; [custom FFmpeg](custom-ffmpeg.md) | software; `vdpau` (`vaapi` not for every camera) | `libx264` |
| Raspberry Pi OS 13 | Raspberry Pi 5 (8 GB; Mesa's V3D driver, OpenGL 3.1) | wheel ([arm64](#linux)) | software (the board decodes HEVC only, and not with the wheel: next line) | `libx264` (it has no encoder) |
| Raspberry Pi OS 13 | the same board, its HEVC decoder | [custom FFmpeg](custom-ffmpeg.md#raspberry-pi-5-the-boards-hevc-decoder) (Raspberry Pi's branch) | `drm` for HEVC cameras (H.265 and H.265+); H.264 cameras in software | `libx264` |
| Raspberry Pi OS 13 | Raspberry Pi 4 B (4 GB; V3D, OpenGL 3.1) | wheel ([arm64](#linux)) | software; `v4l2m2m` (the board's H.264 decoder) where the stream is heavy: [arm64](#linux) | `h264_v4l2m2m`, the board's encoder (with `gpu_mem=256`: [arm64](#linux)); `libx264` |
| Raspberry Pi OS 13 | Raspberry Pi 3 B+ (1 GB; VC4, OpenGL 2.1) | wheel ([arm64](#linux)) | software; `v4l2m2m` for a stream at 25 fps: [arm64](#linux) | `h264_v4l2m2m`, the board's encoder, for two cameras |

Not run on hardware: NVIDIA on Windows, Apple Silicon outside CI, an ARM board other than the Raspberry Pi 3 B+, 4 and 5 (the arm64 install runs on GitHub's ARM runners, without a camera), AV1 encoding on an Intel or AMD GPU (the ones above have no AV1 encoder).

The Linux commands of the next section are also run as written, by an ordinary user, in a fresh image of every distribution named there (Python 3.12 to 3.14; Ubuntu and Debian on arm64 as well; Debian 12 and Ubuntu 22.04 with a Python from uv, Debian 12 on arm64 with PySide6 6.8.0.2 besides) and with Python 3.15 on Debian 13, each week: install, `nova doctor`, a recording through the launcher, a window on a virtual screen and the tests.

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
- **Older systems** (a C library before glibc 2.34: Debian 11, Ubuntu 20.04, RHEL 8): PySide6 6.12 has no wheel for them and `pip` ends with `No matching distribution found for PySide6_Essentials==6.12.0`. PySide6 6.9.3 is the newest that installs there; with a Python from uv as in the point above (RHEL 8 has `python3.12` by name, and takes RHEL 9's package line):

  ```bash
  sed 's/==6\.12\.0/==6.9.3/' requirements.txt > requirements-old.txt
  pip install -r requirements-old.txt
  ```

  Run on a Debian 11 that had never been updated (an Atom D525 nettop of 2010 with NVIDIA ION graphics on the `nouveau` driver, OpenGL 3.3): the install, `nova doctor`, the tests, windows with and without `--opengl`, recording. The same lines were run once in `debian:11`, `ubuntu:20.04` and `rockylinux:8` images. Most of these systems are past their distribution's support, and so is this combination: it is what worked in November 2026, not something the weekly runs repeat.
- **A CPU without SSE4.2** (Intel before the Core i series, Atoms before 2013, AMD before the FX series; `grep -c sse4_2 /proc/cpuinfo` prints 0): two of the wheels are built for it, PySide6 from 6.10 on and numpy from 2.4 on, and an older CPU ends whatever imports them with `Illegal instruction`. Nova looks at the CPU before it imports them, and `python nova.py`, `./nova start` and `nova doctor` name what to install instead: `pip install PySide6_Essentials==6.9.3 numpy==2.3.5` after the requirements (PySide6 6.9.3 is for Python up to 3.13). Run on the Atom D525 above under Debian 11, and under Debian 13 with the lines at the top of this section and its own Python 3.13. On such a CPU one thread may be too little for a camera's stream: [Performance tips](troubleshooting.md#performance-tips).
- **arm64** (Raspberry Pi 3 B+, 4 and 5 with a 64-bit system, other ARM boards and servers): `av`, numpy and PySide6 have aarch64 wheels, and the apt lines above are run on arm64 Ubuntu 24.04 and Debian 13 (which Raspberry Pi OS 13 is) on GitHub's ARM runners, with the tests. 32-bit ARM systems have no wheels. On a Raspberry Pi 5 (8 GB, Raspberry Pi OS 13 with its desktop, November 2026) the lines at the top of this section ran as they are, in two minutes: most of that is `pip` compiling PyAudio and PyOpenGL-accelerate, which have no arm64 wheel. Then `nova doctor`, the tests, windows of real cameras and recording. The same on a Raspberry Pi 4 (4 GB) in four and a half minutes and on a Raspberry Pi 3 B+ (1 GB) in nine.
  - *An arm64 system before glibc 2.39* (Raspberry Pi OS 12 and Debian 12: glibc 2.36, Python 3.11): on arm64 the PySide6 wheels need glibc 2.39 from 6.8.1 on, so the two points above apply together: a Python from uv, and the `sed` line of *Older systems* with `6.8.0.2` in place of `6.9.3` (PySide6 6.8.0.2 is for Python up to 3.13). Run in a `debian:12` arm64 image, by the weekly workflow as well; Nova's tests pass with PySide6 6.8.0.2.
  - *Hardware codecs on a Raspberry Pi 5*: the board has one, a decoder for HEVC (H.265). It has no H.264 decoder and no encoder at all.
    - **H.264 cameras** are decoded by the CPU (four Cortex-A76 cores), whatever `--accel` says: leave it out for them. What that costs is two points below.
    - **HEVC cameras** can use the board's decoder, with `--accel drm` and an FFmpeg of Raspberry Pi's own. The wheel cannot drive it: the decoder speaks V4L2's stateless (request) interface, which FFmpeg has no part for and Raspberry Pi's branch of FFmpeg has. [custom-ffmpeg.md](custom-ffmpeg.md#raspberry-pi-5-the-boards-hevc-decoder) builds that FFmpeg, and `av` against it, on the board in twelve minutes. With it, in a window with `--opengl`, a 2160p stream at 20 fps took 29 % of a core in place of 122 % and a 1080p camera at 25 fps 12 % in place of 22 %, in H.265 and in the camera's H.265+ mode alike; three HEVC streams at once shared the decoder and were in time. A camera whose stream is changed from H.264 to H.265 gets the decoder at its next connection, and an H.264 camera that is given `--accel drm` is decoded by the CPU with one line that says so.
    - `--accel v4l2m2m` is not for this board. It names the other kind of V4L2 codec (`h264_v4l2m2m`, `hevc_v4l2m2m`, …), which the arm64 wheel has and which need a board whose kernel offers such a device (a Raspberry Pi 4 has one for H.264). On a Pi 5 it is answered once (`cannot work here: this machine has no V4L2 decoder that takes a camera's stream`), and `nova doctor` says the same of the `..._v4l2m2m` codecs it lists.
    - **Recording** is `libx264` on the CPU.
  - *`--opengl`* is what makes a Raspberry Pi a machine for several cameras. The GPU driver of a Pi 4 and 5 offers OpenGL 3.1, and an `--opengl` window draws in that (under XWayland, which is the default on the Pi's desktop, and as a native Wayland window): the GPU converts and scales the pictures. Without it the CPU does both for every picture, which on a Pi costs as much as decoding it. A Pi 3 B+'s driver offers OpenGL 2.1, and an `--opengl` window draws in that as well (the same shaders in the older shader language).
  - *What a Raspberry Pi 5 does* (measured with 1080p H.264 cameras, in percent of one of its four cores; the board had its fan and stayed at 60 °C): a camera at 10 fps in a window takes 9 % with `--opengl` and 14 % without, one at 20 fps 20 % and 28 %, one at 25 fps and 15 Mbit/s 41 % and 51 %. Six cameras at once (four at 10 fps, the 20 and the 25 fps one) took 110 % together with `--opengl`, a quarter of the board, and 264 % without it. Thirteen windows with `--opengl` (those six and seven more of the 10 fps kind) took 268 %, two thirds of the board, and 3.3 GB. Recording a 10 fps camera with `libx264` takes 93 % of a core at the default preset, 46 % with `--ffmpeg "-preset veryfast"` and 30 % with `ultrafast` (whose files are twice the size); every picture was in the file each time. HEVC decoded by the CPU (the wheel) costs about twice what H.264 does: 1080p at 10 fps 16 % with `--opengl`, 2160p at 20 fps and 8 Mbit/s 122 % on two decoder threads (187 % on three without `--opengl`); those two were test streams from an NVIDIA encoder, not a camera's. The board's own decoder brings the 2160p stream to 29 % (the point above).
  - *Memory*: every camera is a process of its own: 240 to 260 MB showing a picture, 200 to 370 MB recording without a window and up to 470 MB with one (measured on the Pi 5; a window on a Pi 4 or 3 B+ takes 230 to 250 MB). Count a board's memory in that: the 13 windows above took 3.3 GB, and a board with 1 GB has room for three.
  - *What a Raspberry Pi 4 does* (4 GB, four Cortex-A72 cores; the same 1080p cameras, in percent of one core): a camera at 10 fps takes 21 % with `--opengl` and 25 % without, one at 20 fps 43 % and 50 %, an HEVC camera at 25 fps 45 % and 56 %: about twice what they cost a Pi 5. Four cameras at 10 fps took 99 % together with `--opengl`, a quarter of the board. The six cameras that took a quarter of a Pi 5 filled the Pi 4 (336 %): pictures came late, and each camera's log said `this machine's CPU is busy`.
  - *Hardware codecs on a Raspberry Pi 4*: an H.264 decoder and an H.264 encoder, both of the kind the wheel drives (`h264_v4l2m2m`).
    - **Set `gpu_mem=256` first.** The two work in the memory the board keeps for its firmware, 76 MB as the board comes, and that is room for two 1080p cameras between them: two decoded, or two recorded, or one that is decoded and recorded. Every camera after that is decoded and encoded by the CPU. With this line at the end of `/boot/firmware/config.txt` (under `[all]`) and a restart, five cameras were decoded and recorded by the board at once, and the CPU was what ran out first:

      ```ini
      gpu_mem=256
      ```

      `vcgencmd get_mem gpu` prints the value in use (`gpu=256M`); the memory is taken from the 4 GB of the board. `nova status` shows under each camera whether it got the decoder and the encoder it asked for (a line that begins with `!` where it did not).
    - **Recording without an encoder** is the cheapest by far: `-e copy` writes each camera's video as the camera sends it ([Recording](recording.md#recording-without-an-encoder)). A camera at 10 fps recorded without a window (`nova record cam -e copy`) took 3.8 % of a core (most of it for its sound; 2.1 % with `--ffmpeg "-an"`), and an HEVC camera at 25 fps without sound 2.8 %; six cameras at once took 28 % together, a fourteenth of the board, with every picture in the files. In an `--opengl` window a camera at 10 fps took 22 % shown and recorded this way, against 20.5 % shown alone.
    - **Recording through the board's encoder** is for a file with another bit rate or frame rate than the camera's: `-e h264_v4l2m2m` in a camera's `opts` (the CPU decodes, the board encodes). A camera at 10 fps, shown with `--opengl` and recorded, took 36 % of a core, and 22 % recorded without a window (`nova record`); `libx264` takes 235 % for it at its default preset, 111 % with `--ffmpeg "-preset veryfast"` and 73 % with `ultrafast`. Every picture was in the file each time. Without a `-b:v` the file gets 0.1 bit per pixel and frame (2 Mbit/s for 1080p at 10 fps). Four cameras at 10 fps shown and all recorded this way took 314 %, four fifths of the board, with every picture in the files: on a busy board each camera costs twice what it costs alone. A camera that finds the encoder full gets one line in its log (`h264_v4l2m2m opens and takes no picture … using libx264`), and the CPU encodes it, which a busy Pi 4 does not keep up with at the default preset (the log counts the pictures it dropped).
    - **Decoding** with `--accel v4l2m2m` works, and what it saves depends on the stream. The board decodes a picture with half a millisecond of CPU; the picture then lies in memory of the device, which the CPU reads slowly: 14 ms to take a 1080p picture out, whatever is in it. A stream at 25 fps and 15 Mbit/s, which costs the CPU 142 % of a core on two decoder threads, took 60 % with it. A camera at 10 fps and 2 Mbit/s, which the CPU decodes in 11 ms a picture, took 21 % with it and 21 % without, and four of those 148 % against 99 %. So give it to a camera whose stream is heavy, and not to all of them in `[defaults]`. A camera that finds the decoder full gets no answer from the board: after five seconds the camera's log says `--accel=v4l2m2m cannot work here: h264_v4l2m2m has not answered …`, and the CPU decodes it.
    - **HEVC cameras** can use the board's HEVC decoder, which is the Pi 5's, with `--accel drm` and the FFmpeg of [custom-ffmpeg.md](custom-ffmpeg.md#raspberry-pi-5-the-boards-hevc-decoder) (built on the board in 36 minutes): a 1080p camera at 25 fps took 34 % of a core with `--opengl` in place of 47 %.
  - *What a Raspberry Pi 3 B+ does* (1 GB, four Cortex-A53 cores, a 1080p screen): one camera at full screen, or two or three in windows, with `--opengl`.
    - **At full screen** (1920x1080, `F` or a double click): a 1080p camera at 10 fps takes 40 % of a core, every picture in time. At 25 fps the camera's codec decides what is left of the board. H.264 is what the board decodes itself: a stream took 71 % with `--accel v4l2m2m`, at 2 Mbit/s as at 4 (what its decoder costs does not grow with the bit rate), and 141 % and 185 % in software. HEVC (H.265) has no decoder on this board and is the CPU's work, and more of it than H.264: the same picture took 216 % at 2 Mbit/s and 299 % at 4 Mbit/s, three quarters of the whole board. An HEVC camera that sent 265 kbit/s, a still scene at night, took one core. A 25 fps camera that offers both is better set to H.264 for this board.
    - **In windows**: a camera at 10 fps takes 37 % with `--opengl` and 68 % without; two take 88 % together (a fifth of the board) where without it the CPU no longer converted the pictures in time; three take 174 % and most of the board's memory (a window is 230 MB). A camera at 20 fps takes a core by itself.
    - **Recording without an encoder** (`-e copy`, [Recording](recording.md#recording-without-an-encoder)): a camera at 10 fps recorded without a window took 6 % of a core, four of them 26 % together (and 570 MB of the board's memory: a camera's process is 140 MB without a window). Shown with `--opengl` and recorded, a camera took 41 %, against 39 % shown alone, in a window as at full screen.
    - **Recording through the board's encoder**, as on the Pi 4 (`-e h264_v4l2m2m`), where the file is to have another bit rate than the camera's: a camera at 10 fps shown with `--opengl` and recorded took 57 %, every picture in the file, and two recorded without a window 47 % each. The room is the Pi 4's as well: as the board comes (`gpu_mem` 76 MB) its decoder and encoder take two cameras between them, and with `gpu_mem=128` in `/boot/firmware/config.txt` two cameras were each decoded and recorded by the board, at the price of 50 MB of the board's memory.
    - The desktop of this board puts the screen together on the CPU (Raspberry Pi OS starts its compositor that way for this GPU), so Nova's windows ask for no alpha channel there: what a picture does not cover is plain black, not black at 80 %. With the alpha channel the desktop took a core for one camera at full screen and the pictures came late.
    - The GPU's textures end at 2048 pixels: a camera whose picture is wider says so in its window, and is to be started without `--opengl` there, or with its second stream.
- PyAudio has no Linux wheel and is compiled during `pip install`: that is what `build-essential`, `python3-dev` and `portaudio19-dev` are for (without a compiler: `No such file or directory: 'x86_64-linux-gnu-gcc'`). The `lib…` packages are what Qt loads at run time and does not bring itself; a desktop has most of them already, a server, a container or WSL does not (`ImportError: libglib-2.0.so.0`, or `Could not load the Qt platform plugin "xcb"`). On openSUSE the same libraries are packaged one by one, hence `libgthread-2_0-0` and `krb5` (`ImportError: libgthread-2.0.so.0`, and `libgssapi_krb5.so.2` for Qt's network module, which the launcher's cameras need). `nova doctor` names a Qt module that does not load and the library it misses. Every package line here is run as written in a fresh image of its distribution by the [Install guide](../.github/workflows/install-guide.yml) workflow.
- **NVIDIA**: the proprietary driver is all that is required (`libnvcuvid.so`, `libnvidia-encode.so` come with it); the CUDA toolkit is not needed. Use `--accel cuda`. The wheel's NVENC needs **driver 570.0 or newer** (`The minimum required Nvidia driver for nvenc is 570.0` in the log otherwise): Ubuntu 26.04 ships that, on 24.04 pick `nvidia-driver-570` or newer rather than the 535/550 series. With an older driver Nova still runs and records, but encodes with libx264 on the CPU.
- **An old NVIDIA card** (GeForce 8 to 300 series, ION: NVIDIA's driver for them ended with the 340 series in 2019, and most systems run them on `nouveau`): `--accel cuda` is not for these cards. The decoder FFmpeg uses (NVDEC) is part of NVIDIA's current drivers, and Nova says so once and decodes on the CPU (`--accel=cuda cannot work here: the NVIDIA card runs on the nouveau driver …`). The card can still decode H.264, through VA-API or VDPAU, which the wheel does not have and the [custom build](custom-ffmpeg.md) does (with `libva-dev` and `libvdpau-dev` installed). Both ways were run on an NVIDIA ION (GT218) of 2010 under Debian 11:
  - **`nouveau`** needs the card's video firmware, which it may not ship. The nouveau project's [VideoAcceleration](https://nouveau.freedesktop.org/VideoAcceleration.html) page has the steps: their `extract_firmware.py` on NVIDIA's 325.15 driver file (nothing of it is installed), and the files it writes into `/lib/firmware/nouveau`; no reboot. `vainfo` then lists `VAProfileH264High`, and `--accel vaapi` or `--accel vdpau` decode. Without the firmware the log has `No support for codec h264 profile 77`.
  - **NVIDIA's 340.108 driver**, which no distribution of today packages (NVIDIA's installer, and kernel patches for anything newer than Linux 5.4): `--accel vdpau` decodes; there is no VA-API with it. Its `libGL.so.1` is from before libglvnd, which Nova's windows take into account.

  What it gives, for a 1080p camera at 10 fps in a window with `--opengl`, in percent of one Atom D525 core: software decoding 37 on `nouveau` and 30 on NVIDIA's driver; VDPAU on `nouveau` 22, VA-API on `nouveau` 27, VDPAU on NVIDIA's driver 51 (the pictures come back from the card to be drawn). So `--accel vaapi` on `nouveau` is the one worth having there, and software with `--opengl` is not far from it. The limits: a card of that age decodes one such camera, a second one only at times (Nova moves the camera that falls behind to the CPU); one at 25 fps and 15 Mbit/s is beyond it (57 ms a picture), which Nova notices, says, and decodes on the CPU. FFmpeg's VDPAU refuses a full-range stream (`Unsupported sw format: yuvj420p`) and needs an X display, so not `nova record` over ssh; VA-API over ssh needs the user in the group `render`. There is no encoder on either driver: recordings go through `libx264`, and the log says so. `nova doctor` has a line for each of these cases. The machine was then upgraded to Debian 13 (Mesa 25, Linux 6.12; the firmware stays where it is, FFmpeg has to be built again): VA-API refused two of the three makes of camera there that it had decoded under Debian 11 (`Failed to upload decode parameters: 2 (resource allocation failed)`), and VDPAU decodes as before when its decoder opens, which it did at about one start in three (`Failed setup for format vdpau: hwaccel initialisation returned error` at the others, also right after a reboot). In both cases Nova decodes on the CPU, says why, and asks the card again after five minutes; so the other of the two is worth a try where one fails, and neither is something to count on with that driver.
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

**Smart App Control stops this option.** A fresh Windows 11 has Smart App Control in evaluation mode and may switch it on by itself; from then on Windows loads only program files that are signed or widely known, and MSYS2's are not signed. On the test PC that happened nine days after the install: every camera ended with `ImportError: DLL load failed while importing QtCore: An Application Control policy has blocked this file` (PySide6, numpy and the compiled `av` alike), and MSYS2's own `sleep` with `Permission denied`. Option A ran on with it on. `nova doctor` shows the state in its OS line (`Smart App Control evaluation`, `on`) and says on MSYS2 what is coming. It is switched off in Windows Security, under App & browser control.

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

Checked on Apple Silicon by the CI and, on 2026-11-10, on an Intel iMac (2019, macOS 15.7, MacPorts) with exactly these commands (the tests and a camera in a window again on 2026-11-20, macOS 15.8.3).

What VideoToolbox accelerates is decided by the Mac, not by the wheel: H.264 on every supported Mac, HEVC decode/encode on Apple Silicon and on Intel Macs from 2016 (6th-generation Core) on. On an older Intel Mac one direction may fall back to software while the other is accelerated; the log lines `HW decoder:` / `software decoder` and `[AVWriter]` show which.

`--opengl` requests the 3.3 core-profile context macOS requires; without it the QPainter renderer is used.

Every camera window is a process of its own, so the Dock shows one Nova icon per open window; their name there is Python, the program that runs them.

## Updating

```bash
cd nova && git pull
. venv/bin/activate && pip install -r requirements.txt
```

`requirements.txt` pins versions that are known to work together, except `av`: it names the oldest PyAV Nova supports (`av>=19.0.2`, the first release that hands output options to the protocol), and newer releases are welcome. `pip install -U av` takes the newest; `nova doctor` warns only about an older one. On a machine with a source-built `av` (MSYS2, [custom FFmpeg](custom-ffmpeg.md)), `pip install -U` replaces that build with the newest wheel: build the new release from source instead.
