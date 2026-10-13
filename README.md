# Nova

**One window per camera.** Nova is a lightweight, GPU-accelerated viewer for RTSP/ONVIF cameras that also records, live-streams and plays audio. It is built for people who keep a wall of IP cameras open on an ordinary desktop all day: every camera is an independent, frameless, always-sharp window you can drag, snap to a grid, zoom into, record and push to YouTube with one keystroke.

![Nova camera windows spread over a desktop next to a browser and terminals](docs/images/wall.jpg)
*Part of a 14-camera wall on one PC: every camera is its own frameless window, placed where you want it between your other windows.*

![A live camera window with the control strip](docs/images/nova-window.jpg)
*One window at 1:1, a live 1080p camera through the OpenGL path. The mouse is on the control strip at the top left: live stream (on), record (on), audio (on) and help; the thin orange line is the focus border (`-b`).*

Python · PySide6 (Qt 6) · PyAV (FFmpeg) · PyOpenGL · PyAudio — Linux, Windows, macOS · 0BSD licence.

## Documentation

| | |
|---|---|
| [Installation](docs/install.md) | Linux / Windows / macOS, what the PyPI FFmpeg includes, GPU drivers |
| [User guide](docs/user-guide.md) | `cameras.ini`, the `nova` launcher, every command-line option, **all keyboard and mouse controls**, layouts, zoom, audio, snapshots, where settings live |
| [Recording](docs/recording.md) | segments, fragmented MP4, timestamps and A/V sync, encoder selection, tuning, housekeeping |
| [Live streaming](docs/streaming.md) | YouTube RTMP and HLS (HEVC), Twitch, SRT, MPEG-TS, bitrate/latency, checklist |
| [FFmpeg options](docs/ffmpeg-options.md) | what `--ffmpeg "…"` can and cannot do, measured per encoder, with recipes |
| [Troubleshooting](docs/troubleshooting.md) | log messages explained, performance tips, known limitations |
| [Custom FFmpeg](docs/custom-ffmpeg.md) | building FFmpeg yourself for SRT or VAAPI |
| [Changelog](CHANGELOG.md) | |

## Quick start

```bash
git clone https://github.com/ANDYwyLEE/nova.git && cd nova
python3 -m venv venv && . venv/bin/activate        # Windows: py -m venv venv && venv\Scripts\activate
pip install -U pip && pip install -r requirements.txt   # Linux first: sudo apt install portaudio19-dev

python nova.py --opengl --accel cuda -b -i "rtsp://user:pass@192.168.1.10:554/stream_0"
```

Press **H** in the window for the key map, **R** to record, **A** for audio, **S** for a snapshot. Add `-t rtmp://a.rtmp.youtube.com/live2/YOUR-KEY` and the camera goes live with its first frame; **T** switches the stream off and on.

More than one camera: put them in `cameras.ini` (git-ignored, so passwords stay on your machine) and use the launcher.

```bash
cp cameras.example.ini cameras.ini      # one [section] per camera; presets per OS/GPU in [defaults]
./nova start                            # a window per enabled camera (Windows: nova start)
./nova record                           # the same without windows, recording from the first frame
./nova run frontdoor -r                 # one camera in this terminal; extra args go to nova.py
./nova layout save work                 # remember the whole wall; ./nova start --layout work brings it back
./nova restart                          # stop and start again, each camera the way it was launched
./nova stop
```

## Why Nova

- **Independent windows, independent processes.** Each camera is its own OS process and frameless window. A camera that dies, reconnects or decodes garbage never touches the others; fifteen 1080p cameras on one PC is the normal case.
- **Hardware decoding that is honest.** `--accel cuda|qsv|vaapi|videotoolbox|d3d11va|dxva2` uses the dedicated decoder or the platform hwaccel; if it cannot deliver frames, Nova falls back to multi-threaded software decoding and says so in the log.
- **Zero-copy OpenGL rendering.** NV12/YUV planes go straight into textures and are converted in a shader (BT.601/BT.709 per stream); overlays are cached. The CPU never touches pixels on the display path.
- **Recordings you can trust.** Fragmented MP4 segments survive crashes; timestamps come from the camera clock, so audio and video stay in sync even when a camera lies about its frame rate, and outages become gaps instead of fast-forward.
- **Streaming that matches the target.** H.264 for RTMP (what YouTube accepts), H.264/HEVC for HLS, 2-second keyframes, a silent audio track when the camera has none.
- **Encoders chosen for you.** With `--accel`, the matching hardware encoder (NVENC, Quick Sync, AMF, VideoToolbox), else libx264; probed once per process under a cross-process lock.
- **Real FFmpeg option pass-through.** `--ffmpeg "-rtsp_transport udp -b:v 4M -vf transpose=1 -r 10"` is routed to the right place, and anything the library cannot honour is reported instead of silently dropped.
- **Camera discovery.** `onvif://`, `xmeye://`, `hik://`, `dahua://` resolve the RTSP URL for you; results are cached without the password.
- **Built for camera walls.** Grid snapping, keyboard move/resize, wheel zoom with remembered regions, fullscreen, per-camera geometry restored on the next start, multi-monitor aware.
- **Small and transparent.** About 5k lines of Python; no ffmpeg binary, no GStreamer, no database. The PyPI `av` wheel already bundles FFmpeg with NVENC/NVDEC, Quick Sync and AMF.

## Feature overview

| Area | What you get |
|---|---|
| Inputs | RTSP (TCP/UDP), RTMP, SRT*, HTTP/HLS, MPEG-TS over UDP, files, capture devices (`-i 0`, `dshow://video=NAME`, `v4l2:///dev/video2`), `onvif://` `xmeye://` `hik://` `dahua://` |
| Decoding | software (multi-threaded), `cuda` (NVDEC/CUVID), `qsv`, `vaapi`*, `videotoolbox`, `d3d11va`, `dxva2`, `rkmpp`/`v4l2m2m` (SBCs) |
| Display | OpenGL shader path (NV12/YUV direct), QPainter fallback, frame-rate cap, zoom, fullscreen, focus-coloured borders |
| Recording | fragmented MP4 segments aligned to the clock, camera-clock timestamps, NVENC/QSV/AMF/VideoToolbox/x264, `-vf` filters, `-r` decimation |
| Streaming | RTMP (YouTube, Twitch), HLS (YouTube HLS ingest incl. HEVC), SRT*, MPEG-TS; silent-audio synthesis |
| Audio | PortAudio playback with device selection and resampling fallback |
| Discovery | ONVIF (port probing, vendor-agnostic SOAP), XMeye DVRIP, Hikvision ISAPI, Dahua CGI |
| Operations | `nova` launcher (`start`, `record`, `stop`) with a git-ignored camera list, auto-reconnect, bounded I/O timeouts, clean SIGINT/SIGTERM shutdown, masked passwords, pre-commit secret guard |

\* SRT and VAAPI need a [custom FFmpeg build](docs/custom-ffmpeg.md); everything else works with the PyPI wheel.

## How it works

```mermaid
flowchart LR
    CAM[("RTSP / ONVIF<br/>camera")] -->|av.open| VW["VideoWorker<br/>QThread: demux + decode"]
    VW -->|"DisplayFrame, newest frame only"| GL["NovaGL / Nova<br/>QOpenGLWindow"]
    VW -->|"frames, zero-copy"| RQ[rec_q] --> REC["AVRecWorker<br/>AVWriter: MP4 segments"]
    VW -->|frames| SQ[stream_q] --> STR["AVRecWorker<br/>AVWriter: RTMP / HLS / SRT"]
    VW -->|PCM| AQ[audio_q] --> AUD["AudioWorker<br/>PortAudio"]
    GL -->|"keys, mouse"| CORE["NovaCore<br/>signals, toggles, snapshots"]
    CORE --> REC & STR & AUD
```

One `nova.py` process is one camera. The decoder thread hands frames to the GUI through a one-slot queue (always the newest frame, never a backlog) and to the recorder/streamer queues without copying. Encoders run on their own threads, so a slow disk or a stalled RTMP server never blocks decoding or the window.

| File | Role |
|---|---|
| `nova.py` | command line, logging, Qt application, shutdown |
| `nova`, `nova.bat`, `cams.py`, `cameras.example.ini` | the launcher (`nova start / record / run / stop / restart / status / list / layout`), its implementation and the camera-list template (`cameras.ini` is git-ignored) |
| `novacore.py` | per-camera controller: worker, recorder, streamer, audio, snapshots |
| `videoworker.py` | connect / decode thread, HW decoder selection, URI resolution, frame fan-out |
| `view.py`, `novagl.py` | frameless window, OSD, geometry, zoom; OpenGL shader renderer |
| `avwriter.py` | encoder selection, timestamps, `-vf` graph, muxing to files and live targets |
| `recorder.py`, `streamer.py`, `recworker.py` | segment rotation, stream lifecycle, encoder threads |
| `audioworker.py`, `audioplayer.py` | PortAudio playback |
| `auth.py` | ONVIF / XMeye / Hikvision / Dahua URL resolution |
| `ffparser.py` | `--ffmpeg` string → input / decoder / encoder / muxer options |
| `dispframe.py`, `zoomer.py`, `mousehandler.py`, `keyhandler.py`, `helper.py` | display frame container, zoom math, input handling, help overlay (`docs/gen_keys.py` builds the manual's tables from it) |

## Development notes

- Any file FFmpeg can open works as `-i`, so a test clip replaces a camera. For realistic timing feed it at real time: `ffmpeg -re -stream_loop -1 -i clip.mp4 -c copy -f mpegts udp://127.0.0.1:5004` and `-i udp://127.0.0.1:5004`.
- GUI tests without a desktop: `Xvfb :99 -screen 0 1280x800x24 &` then `DISPLAY=:99 python nova.py --opengl …`. `--headless` runs the full decode/record/stream pipeline with no window.
- After editing the help table in `helper.py`, run `python docs/gen_keys.py` to refresh the manual (`--check` only verifies; CI runs it).
- Tests: `pip install -r requirements-dev.txt` then `pytest`. They need no camera and no GPU: option parsing, URL masking, the launcher's logic, the timestamp mapper, and a headless decode → libx264 → fragmented-MP4 recording of a generated clip. GitHub Actions runs them on Linux for every push and on Windows and macOS weekly (`.github/workflows/`).
- `git config core.hooksPath .githooks` enables the pre-commit guard against committed credentials.

## License

Nova is released under the [BSD Zero Clause License](LICENSE) (0BSD): use, copy, modify and redistribute it for any purpose, with or without attribution. © 2026 ANDYwyLEE.

The licence covers Nova's own source. Its dependencies keep their own terms: PySide6/Qt (LGPL), PyAV, PyOpenGL, NumPy (BSD), PyAudio (MIT). The FFmpeg bundled in the PyPI `av` wheel is built with GPL components (libx264, libx265), so if you ever redistribute Nova *together with* that wheel as a packaged binary, the GPL applies to that bundle.
