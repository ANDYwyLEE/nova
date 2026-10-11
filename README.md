# Nova

**One window per camera.** Nova is a lightweight, GPU-accelerated viewer for RTSP/ONVIF cameras that also records, live-streams and plays audio. It is built for people who keep a wall of IP cameras open on an ordinary desktop all day: every camera is an independent, frameless, always-sharp window you can drag, snap to a grid, zoom into, record and push to YouTube with one keystroke.

![Nova window with the control strip](docs/images/nova-window.png)
*Demo clip rendered through the OpenGL path; the strip shows live-stream, record (on), audio and help.*

Python · PySide6 (Qt 6) · PyAV (FFmpeg) · PyOpenGL · PyAudio — Linux, Windows, macOS · 0BSD licence.

---

## Why Nova

**Independent windows, independent processes.** Each camera runs as its own OS process with its own frameless window. A camera that dies, reconnects or decodes garbage never touches the others. Fifteen 1080p cameras on one PC is the normal use case, not a stress test.

**Hardware decoding that is honest about what it does.** `--accel cuda|qsv|vaapi|videotoolbox|d3d11va|dxva2` picks the dedicated decoder (NVDEC/CUVID, Quick Sync, …) or attaches the platform hwaccel to the standard decoder. If the hardware path cannot deliver frames, Nova falls back to multi-threaded software decoding and *says so* in the log instead of pretending.

**Zero-copy OpenGL rendering.** Decoded NV12 / YUV420P planes are uploaded straight into textures and converted to RGB in a shader (BT.601/BT.709 chosen per stream). Overlays are cached and re-uploaded only when they change. The CPU never touches pixels on the display path.

**Recordings you can trust.** 30-minute segments (configurable) as fragmented MP4, so a crash or power cut loses at most the last two seconds. Timestamps come from the camera's own clock, re-anchored to wall time on reconnects: no audio/video drift when a cheap camera lies about its frame rate, and outages appear as gaps instead of speeding the file up.

**Live streaming that matches the target.** `-t rtmp://a.rtmp.youtube.com/live2/KEY` is enough. Nova picks H.264 for RTMP (YouTube rejects anything else), H.264/HEVC for HLS, a 2-second keyframe interval, and adds a silent AAC track when the camera has no microphone because ingest servers refuse video-only streams.

**Encoders chosen for you.** NVENC, Quick Sync, AMF, VideoToolbox or libx264, probed once per process (with a cross-process lock so fifteen starting cameras do not hammer the GPU). The camera's own codec family is preferred for recordings; `-e hevc_nvenc` forces one.

**Real FFmpeg option pass-through.** `--ffmpeg "-rtsp_transport udp -b:v 4M -vf transpose=1 -r 10"` is routed to the right place (input, decoder, encoder, muxer) with stream specifiers understood. Options the library cannot honour are reported once in the log rather than silently dropped. See [docs/ffmpeg-options.md](docs/ffmpeg-options.md) for the measured list.

**Camera discovery.** `onvif://user:pw@ip` resolves the RTSP URL through ONVIF (port probing, vendor-agnostic SOAP parsing). `xmeye://`, `hik://` and `dahua://` shortcuts cover the common Chinese DVR/NVR families. Resolved URLs are cached for a day without storing the password.

**Window management for camera walls.** Grid snapping, keyboard move/resize, wheel zoom with sub-region persistence, fullscreen, right-click "fit window to video", per-camera geometry remembered across restarts (also on Ctrl-C), and correct behaviour on multi-monitor desktops with a screen left of or above the primary one.

**Small and transparent.** ~5k lines of Python. No ffmpeg binary, no GStreamer, no database. The PyPI `av` wheel already bundles FFmpeg with NVENC/NVDEC, Quick Sync and AMF; a custom FFmpeg is only needed for SRT or VAAPI ([docs/custom-ffmpeg.md](docs/custom-ffmpeg.md)).

## Feature overview

| Area | What you get |
|---|---|
| Inputs | RTSP (TCP/UDP), RTMP, SRT*, HTTP/HLS, MPEG-TS over UDP, files, capture devices (`-i 0`, `dshow://video=NAME`, `v4l2:///dev/video2`), `onvif://` `xmeye://` `hik://` `dahua://` |
| Decoding | software (multi-threaded), `cuda` (NVDEC/CUVID), `qsv`, `vaapi`*, `videotoolbox`, `d3d11va`, `dxva2`, `rkmpp`/`v4l2m2m` (SBCs) |
| Display | OpenGL shader path (NV12/YUV direct), QPainter fallback, frame-rate cap, zoom, fullscreen, borders that show focus |
| Recording | fragmented MP4 segments, auto-rotation at wall-clock boundaries, camera-clock timestamps, NVENC/QSV/AMF/VideoToolbox/x264, `-vf` filters, `-r` frame decimation |
| Streaming | RTMP (YouTube, Twitch), HLS (YouTube HLS ingest incl. HEVC), SRT*, MPEG-TS; silent-audio synthesis |
| Audio | PortAudio playback with device selection and resampling fallback |
| Discovery | ONVIF GetCapabilities/GetProfiles/GetStreamUri with port probing; XMeye DVRIP; Hikvision ISAPI; Dahua CGI |
| Operations | `cams.py` launcher with a git-ignored camera list, auto-reconnect, bounded I/O timeouts, graceful SIGINT/SIGTERM shutdown, passwords masked in logs, pre-commit secret guard |

\* SRT and VAAPI need a custom FFmpeg build; everything else works with the PyPI wheel.

## How it works

```mermaid
flowchart LR
    CAM[(RTSP / ONVIF\ncamera)] -->|av.open| VW[VideoWorker\nQThread: demux + decode]
    VW -->|DisplayFrame\ndeque(1)| GL[NovaGL / Nova\nQOpenGLWindow]
    VW -->|frames, zero-copy| RQ[rec_q] --> REC[AVRecWorker\nAVWriter → MP4 segments]
    VW -->|frames| SQ[stream_q] --> STR[AVRecWorker\nAVWriter → RTMP / HLS / SRT]
    VW -->|PCM| AQ[audio_q] --> AUD[AudioWorker\nPortAudio]
    GL -->|keys, mouse| CORE[NovaCore\nsignals, toggles, snapshots]
    CORE --> REC & STR & AUD
```

One `nova.py` process = one camera. The decoder thread hands frames to the GUI through a one-slot queue (always the newest frame, never a backlog) and to the recorder/streamer queues without copying. Encoders run on their own threads, so a slow disk or a stalled RTMP server never blocks decoding or the window.

## Quick start

```bash
git clone https://github.com/ANDYwyLEE/nova.git && cd nova
python3 -m venv venv && . venv/bin/activate      # Windows: venv\Scripts\activate
pip install -U pip && pip install -r requirements.txt

python nova.py --opengl --accel cuda -b -i "rtsp://user:pass@192.168.1.10:554/stream_0"
```

Press **H** in the window for the key map, **R** to record, **A** for audio, **S** for a snapshot. Add `-t rtmp://a.rtmp.youtube.com/live2/YOUR-KEY` and press **T** to go live.

For more than one camera, put them in `cameras.ini` (git-ignored, so passwords stay on your machine) and use the launcher:

```bash
cp cameras.example.ini cameras.ini     # edit: one [section] per camera with its rtsp:// URL
python cams.py list                    # passwords are masked in all output
python cams.py start                   # every enabled camera, one process each, logs in logs/
python cams.py run frontdoor -r        # one camera in the foreground, extra args go to nova.py
python cams.py stop                    # Ctrl-C semantics: geometry and recordings are saved
```

Linux needs the PortAudio headers for PyAudio: `sudo apt install portaudio19-dev` (see [Installation](#installation)).

## Installation

Nova only needs Python 3.10+ and the packages in `requirements.txt`. The `av` package from PyPI ships its own FFmpeg (checked with av 19.0.1):

| included in the PyPI wheel | not included |
|---|---|
| `h264/hevc/av1_nvenc`, `*_cuvid` (NVIDIA) | SRT protocol (`srt://`) |
| `*_qsv` via libvpl (Intel), `*_amf` (AMD) | VAAPI (Intel/AMD on Linux without NVIDIA) |
| libx264, libx265, libsvtav1, libvpx, aac, opus | `drawtext` filter (no libfreetype) |
| rtmps / https / tls (gnutls) | anything newer than the wheel's FFmpeg |
| videotoolbox (macOS wheel, expected) | |

Check what your installed `av` can do:

```bash
python -c "import av; from av.codec.hwaccel import hwdevices_available as h; print(h()); \
  print(sorted(n for n in av.codecs_available if any(k in n for k in ('nvenc','cuvid','qsv','amf','videotoolbox','vaapi'))))"
```

Need SRT or VAAPI? Build FFmpeg into `~/deps` and compile `av` against it: [docs/custom-ffmpeg.md](docs/custom-ffmpeg.md). Do not run a blanket `pip install -U` on that venv afterwards, it would replace the source-built `av` with the wheel.

### Linux (Ubuntu 24.04)

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-dev portaudio19-dev \
    libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0

cd nova
python3 -m venv python3.12 && ln -s python3.12 venv && . venv/bin/activate
pip install -U pip && pip install -r requirements.txt
python nova.py --help
```

- NVIDIA: the driver alone provides NVDEC/NVENC (`libnvcuvid`, `libnvidia-encode`); the CUDA toolkit is not required. Use `--accel cuda`.
- Intel: `--accel qsv` (needs `intel-media-va-driver-non-free` and the oneVPL runtime; untested here). `--accel vaapi` needs the custom build.

### Windows

**Option A — python.org CPython + PyPI wheels (simplest)**

```bat
py -m venv venv
venv\Scripts\activate
pip install -U pip
pip install -r requirements.txt
```

`s.bat`, `s.ps1` and `startall.bat` look for `venv\Scripts\activate` first and fall back to `venv\bin` (MSYS2). Accel: `cuda`, `qsv`, `d3d11va`, `dxva2`. PyAudio wheels exist on PyPI for current Python versions.

**Option B — MSYS2 UCRT64**

MSYS2's Python cannot use PyPI binary wheels, so `av` is compiled against MSYS2's own FFmpeg package (which already ships NVENC, QSV, AMF and SRT):

```bash
pacman -S --needed mingw-w64-ucrt-x86_64-{gcc,python-pip,python-wheel,python-cython,python-setuptools,python-numpy,python-requests,portaudio,pyside6,python-pyopengl,ffmpeg} make git pkgconf
python -m venv --system-site-packages python3.12 && ln -s python3.12 venv && . venv/bin/activate
pip install xmltodict filelock PyOpenGL_accelerate
pip install --no-binary av av --no-cache-dir
```

PyAudio assumes MSVC on Windows; under MSYS2 download the sdist, add `('MS_WIN64','1')` to `defines`, `/ucrt64/include` and `/ucrt64/lib` to the include/library paths and `portaudio` to the libraries in `setup.py`, then `python setup.py install`.

### macOS (11.7 Big Sur or later)

```bash
xcode-select --install
sudo port install portaudio python312        # or brew install portaudio python@3.12
cd nova
python3.12 -m venv python3.12 && ln -s python3.12 venv && . venv/bin/activate
pip install -U pip
CFLAGS="-I/opt/local/include" LDFLAGS="-L/opt/local/lib" pip install -r requirements.txt
python nova.py --opengl --accel videotoolbox -i rtsp://...
```

The 3.3 core-profile OpenGL context that macOS requires is requested automatically with `--opengl`.

## Running

### Camera list and launcher

`cameras.ini` is the one file you edit. It is listed in `.gitignore`; the committed `cameras.example.ini` shows the format with placeholder credentials:

```ini
[defaults]
; one `opts =` line for every camera; the template lists presets per OS / GPU / log level
opts = --opengl --accel cuda -f 30 -b -l CRITICAL

[env]
; environment for the camera processes
NOVA_TIMEOUT = 10000000
; NOVA_GPU_DEVICES = 0,1

; one section per camera: the section name is the id used on the command line
[frontdoor]
name = Front door
url = rtsp://admin:password@192.168.1.10:554/stream_0

[yard]
url = rtsp://192.168.1.11:554/user=admin&password=password&channel=1&stream=0
; appended to the default opts
opts = -r --record-interval 1h
; skipped by `start`
enabled = no
```

Comments are whole lines starting with `;` or `#`; a `;` after a value would become part of the value.

| Command | What it does |
|---|---|
| `python cams.py list` | cameras, enabled state, options; passwords masked |
| `python cams.py start [ID …] [-- nova args]` | start every enabled (or the named) camera in the background; stdout goes to `logs/ID.log` |
| `python cams.py stop [ID …]` | Ctrl-C the cameras started by `start` (window geometry, zoom and open recordings are saved) |
| `python cams.py status` | which launched cameras are still running |
| `python cams.py run [ID] [nova args]` | one camera in the foreground, e.g. `run frontdoor -r -t rtmp://…` |

`s.sh` / `s.bat` (= `run`), `startall.sh` / `.bat` (= `start`) and `stopall.sh` / `.bat` (= `stop`) are thin wrappers that activate the venv first. The launcher works the same on Linux, Windows and macOS.

A pre-commit hook (`.githooks/pre-commit`) refuses commits that contain real-looking camera passwords or YouTube stream keys; enable it once per clone with `git config core.hooksPath .githooks`.

### Command line

```
python nova.py -i URL [options]

  -i, --input URL        rtsp:// rtmp:// srt:// http(s)://  onvif://user:pw@ip[:port][/0|1]
                         xmeye://  hik://  dahua://  '0' (first capture device)  dshow://video=NAME  v4l2:///dev/videoN
  -n, --name NAME        camera name for recording/snapshot folders (default: host[_port])
  --opengl               shader rendering, zero-copy NV12/YUV upload (recommended)
  --accel BACKEND        cuda | qsv | vaapi | videotoolbox | d3d11va | dxva2 | d3d12va | rkmpp | v4l2m2m | none
  -f, --fps N            display frame-rate cap (default 30)
  -b, --border           coloured border: orange = focused, green = unfocused
  -g, --grid-size N      grid for snapping and keyboard moves (default 20 px)
  -a, --audio            start audio playback immediately
  -r, --record           start recording immediately
  --record-interval T    segment length: 10m, 1h, 3600s (default 30m)
  --output-dir DIR       base folder (default ./videos and ./snapshots)
  --recording-dir DIR    overrides --output-dir for recordings
  --snapshot-dir DIR     overrides --output-dir for snapshots
  -e, --encoder NAME     force an encoder: h264_nvenc, hevc_nvenc, h264_qsv, libx264, ...
  -t, --target URL       live stream target (rtmp://, https://...m3u8, srt://, udp://) or an output file
  --ffmpeg "OPTS"        FFmpeg options, see docs/ffmpeg-options.md
  --headless             no window (recording / streaming server use)
  -l, --log-level LVL    TRACE VERBOSE DEBUG INFO WARNING ERROR, or a number
```

### Keyboard and mouse

| Key | Action | Key | Action |
|---|---|---|---|
| **H** / F1 | help overlay | **F** / F11, double-click | fullscreen |
| **R** / F7 | record on/off | **T** / F8 | stream on/off |
| **A** / F5 | audio on/off | **S** | snapshot (PNG) |
| **B** | toggle border | **Esc** | reset zoom / leave fullscreen |
| arrows | move by one grid step | Ctrl / Alt + arrows | grow / shrink that edge |
| PgUp / PgDn | resize | Home | 1:1 pixel size, centred |
| + / − / I / O | zoom in / out | wheel | zoom around the cursor |
| Ctrl + / Ctrl − | grow / shrink window | **P** | print the stream URL |
| drag | move | drag an edge | resize |
| right-click | fit window to the video (or zoomed region) | middle-click | fit the zoom region to the window |
| Ctrl-C | quit | | |

The control strip (stream / record / audio / help icons) appears when the mouse enters the top-left corner. **H** shows the complete map inside the window:

![Help overlay](docs/images/nova-help.png)

### Environment variables

| Variable | Meaning |
|---|---|
| `NOVA_TIMEOUT` | socket/read timeout in microseconds (default 5 s; the scripts use 10 s). Also bounds shutdown time. |
| `NOVA_GPU_DEVICES` | GPU index or comma list (`0,1`): each process picks one at random, spreading cameras over GPUs |
| `NOVA_AUDIO_DEVICE_INDEX` | force a PortAudio output device |
| `QT_QPA_PLATFORM` | set to `offscreen` automatically for `--headless` |

## Recording

```
videos/<camera>/YYYYMMDD_HHMM_HHMM.mp4
```

- Segments rotate at wall-clock boundaries of `--record-interval` (30-minute files start at :00 and :30). The file being written is `<camera>_output.mp4` and is renamed when it closes; empty segments are deleted.
- Files are fragmented MP4 (`movflags +frag_keyframe+empty_moov`): playable up to the last fragment even after a crash. Prefer classic MP4? `--ffmpeg "-movflags +faststart"`.
- Keyframe every 2 s by default (`-g` to change). HEVC in MP4 is tagged `hvc1` for Apple/YouTube compatibility.
- Video timestamps follow the camera clock; audio stays in sync even when the stream's declared frame rate is wrong. A reconnect leaves a gap in the timeline rather than compressing time.
- Useful variants:

```bash
# 10 fps archive at 1 Mbit/s, camera mounted sideways
python nova.py -i rtsp://... -r --ffmpeg "-r 10 -b:v 1M -vf transpose=1"
# HEVC with NVENC, 1-hour files, mono 16 kHz audio
python nova.py -i rtsp://... -r --record-interval 1h -e hevc_nvenc --ffmpeg "-ac 1 -ar 16000"
```

## Live streaming

```bash
# YouTube / Twitch RTMP (H.264 chosen automatically; audio synthesised if the camera has none)
python nova.py -i rtsp://... -t rtmp://a.rtmp.youtube.com/live2/xxxx-xxxx-xxxx-xxxx

# YouTube HLS ingest with HEVC
python nova.py -i rtsp://... --headless -e hevc_nvenc \
  -t "https://a.upload.youtube.com/http_upload_hls?cid=xxxx-xxxx&copy=0&file=temp.m3u8" \
  --ffmpeg "-f hls -hls_time 2 -hls_list_size 4 -method PUT -vtag hvc1"

# SRT (custom FFmpeg build) or plain MPEG-TS over UDP
python nova.py -i rtsp://... -t "srt://relay:9000?mode=caller&passphrase=secret&pbkeylen=32"
python nova.py -i rtsp://... -t udp://239.0.0.1:5004
```

Streaming and recording can run at the same time; each gets its own encoder instance. The stream starts on **T** or immediately when `-t` is given.

## Passing FFmpeg options

Nova is a *library* program: options reach FFmpeg through the libavcodec / libavformat APIs, so only options that are real AVOptions of a codec, (de)muxer or protocol can take effect. Options that belong to the `ffmpeg` command-line program (`-t -ss -map -re -vsync …`) are reported once and ignored. Everything below was measured with libx264, h264_nvenc, h264 and h264_cuvid.

```bash
--ffmpeg "-rtsp_transport udp"                 # input / protocol
--ffmpeg "-threads 2 -skip_frame nokey"        # decoder (software)
--ffmpeg "-resize 640x360"                     # decoder (cuvid only): GPU-side downscale
--ffmpeg "-b:v 4M -maxrate 4M -bufsize 8M -g 60 -bf 0 -profile:v high"
--ffmpeg "-crf 23 -preset veryfast"            # libx264 / libx265
--ffmpeg "-rc vbr -cq 28 -preset p4"           # nvenc (presets p1..p7, tune hq|ll|ull)
--ffmpeg "-vf transpose=1,scale=640:-2 -r 10 -s 1280x720 -pix_fmt yuv444p"
--ffmpeg "-c:a libopus -b:a 64k -ar 16000 -ac 1"   # audio;  -an removes the track
--ffmpeg "-f mpegts -movflags +faststart -metadata title=Lounge -hls_time 2 -method PUT"
```

Two log lines tell you when something did not apply: `ignored unknown --ffmpeg options: {...}` (the codec did not recognise the key) and `h264_nvenc rejects your --ffmpeg options ... Trying the next encoder` (for example an x264 preset name given to NVENC). The full table, with what each encoder accepts and what is impossible, is in **[docs/ffmpeg-options.md](docs/ffmpeg-options.md)**.

## Troubleshooting

| Log message | Meaning / fix |
|---|---|
| `--accel=X is not supported by this FFmpeg/PyAV build` | the wheel lacks that hwaccel (e.g. vaapi); see the capability check above or build FFmpeg yourself |
| `No working HW decoder for --accel=cuda. Falling back to software` | driver/GPU not usable from this process; check `nvidia-smi`, `NOVA_GPU_DEVICES` |
| `produced no frames after N keyframes; reconnecting with software decoding` | the hardware decoder opened but cannot decode this stream (odd profile/level); software is used from now on |
| `Failed: Connection timed out` overlay | camera unreachable; Nova retries every 10 s. `NOVA_TIMEOUT` controls the socket timeout |
| `-vf: filter 'drawtext' is not available` | the PyPI wheel has no libfreetype; use a custom build or another filter |
| `Audio open failed` | no PortAudio output device; set `NOVA_AUDIO_DEVICE_INDEX` (run with `-l TRACE` to list devices) |
| recordings are AV1 / YouTube shows no video | fixed in the 2026-10-11 release: encoder now follows the container; use `-e` to override |

## Project layout

| File | Role |
|---|---|
| `nova.py` | command line, logging, Qt application, shutdown |
| `cams.py`, `cameras.example.ini` | multi-camera launcher and the camera-list template (`cameras.ini` is git-ignored) |
| `novacore.py` | per-camera controller: worker, recorder, streamer, audio, snapshots |
| `videoworker.py` | connect / decode thread, HW decoder selection, URI resolution, frame fan-out |
| `view.py`, `novagl.py` | frameless window, OSD, geometry, zoom; OpenGL shader renderer |
| `avwriter.py` | encoder selection, timestamps, `-vf` graph, muxing to files and live targets |
| `recorder.py`, `streamer.py`, `recworker.py` | segment rotation, stream lifecycle, encoder threads |
| `audioworker.py`, `audioplayer.py` | PortAudio playback |
| `auth.py` | ONVIF / XMeye / Hikvision / Dahua URL resolution |
| `ffparser.py` | `--ffmpeg` string → input / decoder / encoder / muxer options |
| `dispframe.py`, `zoomer.py`, `mousehandler.py`, `keyhandler.py`, `helper.py` | display frame container, zoom math, input handling, help overlay |

## Development notes

- Any file FFmpeg can open works as `-i`, so a test clip replaces a camera. For realistic timing feed it at real time: `ffmpeg -re -stream_loop -1 -i clip.mp4 -c copy -f mpegts udp://127.0.0.1:5004` and `-i udp://127.0.0.1:5004`.
- GUI tests without a desktop: `Xvfb :99 -screen 0 1280x800x24 &` then `DISPLAY=:99 python nova.py --opengl …`. `--headless` runs the full decode/record/stream pipeline with no window.
- `-l DEBUG` shows the decoder/encoder negotiation; `-l TRACE` adds ONVIF SOAP bodies and audio device lists. Passwords are masked in all log output.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

Nova is released under the [BSD Zero Clause License](LICENSE) (0BSD): use, copy, modify and redistribute it for any purpose, with or without attribution. © 2026 ANDYwyLEE.

The licence covers Nova's own source. Its dependencies keep their own terms: PySide6/Qt (LGPL), PyAV, PyOpenGL, NumPy (BSD), PyAudio (MIT). The FFmpeg bundled in the PyPI `av` wheel is built with GPL components (libx264, libx265), so if you ever redistribute Nova *together with* that wheel as a packaged binary, the GPL applies to that bundle.
