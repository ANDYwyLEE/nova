# Nova

**One window per camera.** Nova is a lightweight, GPU-accelerated viewer for RTSP/ONVIF cameras that also records, live-streams and plays audio. It is built for people who keep IP cameras open on an ordinary desktop all day, next to their work: every camera is an independent, frameless, always-sharp window you can drag, snap to a grid, zoom into the part that matters, record and push to YouTube with one keystroke, and a layout brings the whole wall back tomorrow.

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
cp cameras.example.ini cameras.ini && chmod 600 cameras.ini   # one [section] per camera (it holds passwords)
./nova start                            # a window per enabled camera (Windows: nova start)
./nova record                           # the same without windows, recording from the first frame
./nova run frontdoor -r                 # one camera in this terminal; extra args go to nova.py
./nova layout save work                 # remember the whole wall; ./nova start --layout work brings it back
./nova status                           # what every camera is doing now (recording, stream, picture)
./nova restart                          # stop and start again, each camera the way it was launched
./nova stop
./nova doctor                           # versions, drivers, FFmpeg build, masked config: what a bug report needs
```

## Why Nova

- **Cameras that live beside your work.** Each camera is a plain frameless window on your normal desktop: drag it between the browser and the terminal, snap it to the grid, move and resize it from the keyboard, park it on the second monitor, go full-screen for a moment and come back to the exact same place. The fixed grid of whole frames that NVR clients give you is the one layout Nova does not impose.
- **Watch the part that matters.** The zoom is a *region* of the picture, not a magnification: wheel or `I`/`O` into the gate, the till or the nest box, pan it with Ctrl-drag or Shift+arrows, and a right click gives the window the region's own shape. Resizing the window keeps the region and reveals the picture around it instead of black bars; the region survives reconnects and restarts. A 4K camera can show just its door in a 300-pixel window all day.
- **Layouts.** `nova layout save work` remembers which cameras are open, where every window sits and which part of the picture each one shows; `nova start --layout work` brings the wall back, `nova layout apply work` moves the open windows there without a restart, and `nova layout export` turns it into window rules for sway, KWin or Hyprland. Every window remembers its own place by itself too, monitor changes included.
- **Independent windows, independent processes.** Each camera is its own OS process. A camera that dies, reconnects or decodes garbage never touches the others; fifteen 1080p cameras on one PC is the normal case. A control channel per camera lets `nova status` show what each one is doing right now and `nova stop` end it cleanly, geometry, zoom and open recordings saved.
- **Hardware decoding that is honest.** `--accel cuda|qsv|vaapi|videotoolbox|d3d11va|dxva2` uses the dedicated decoder or the platform hwaccel, one hardware context per camera (430–460 MiB of VRAM and 4–11 % of one core per live 1080p H.264 camera on an RTX 4090, the bitrate deciding; measured on a 13-window wall, [install.md](docs/install.md)). If it cannot deliver frames, Nova falls back to multi-threaded software decoding, says so in the log, and tries the hardware again later.
- **Zero-copy OpenGL rendering.** NV12/YUV planes go straight into textures and are converted in a shader (BT.601/BT.709 per stream); overlays are cached. The CPU never touches pixels on the display path.
- **Recordings you can trust.** Fragmented MP4 segments aligned to your local clock survive crashes, and a segment cut short by a crash or a power cut is kept as a `_recovered` file. Timestamps come from the camera clock, so audio and video stay in sync even when a camera lies about its frame rate, and outages become gaps instead of fast-forward. A full disk or a failed encoder is retried, not given up on.
- **Streaming that matches the target.** H.264 for RTMP (what YouTube accepts), H.264/HEVC for HLS, 2-second keyframes, a silent audio track when the camera has none, and reconnection with backoff when the server drops the connection. A night of 1080p25 to YouTube with recording on the side ran at a flat 1 GB of memory and a fifth of a core.
- **Encoders chosen for you.** With `--accel`, the matching hardware encoder (NVENC, Quick Sync, AMF, VideoToolbox), else libx264; probed once per process under a cross-process lock, and swapped for the next candidate if it fails at the real size.
- **Real FFmpeg option pass-through.** `--ffmpeg "-rtsp_transport udp -i -b:v 4M -vf transpose=1 -r 10"` is routed by FFmpeg's own option tables: every option goes to the demuxer, decoder, encoder or muxer that defines it, `-i` splits input from output options as on an `ffmpeg` command line, and options that only the `ffmpeg` program has are reported with what to use instead.
- **Camera discovery.** `onvif://`, `hik://`, `dahua://`, `xmeye://`, `reolink://` and `axis://` ask the device for its RTSP port and stream URL, `tapo://` and `unv://` know the maker's path, and `/CHANNEL/STREAM` picks a recorder channel (`hik://admin:pw@nvr/3/sub`); results are cached without the password.
- **Your secrets stay yours.** `cameras.ini` is git-ignored and read by each camera process itself, so a password or stream key never appears on a command line (`ps`), in a log (masked) or in a file others can read: recordings, snapshots, logs, layouts and settings are created private. A pre-commit guard refuses to commit credentials.
- **Small and transparent.** Under 10k lines of Python in 28 files; no ffmpeg binary, no GStreamer, no database, no daemon. The PyPI `av` wheel already bundles FFmpeg with NVENC/NVDEC, Quick Sync and AMF.

## Feature overview

| Area | What you get |
|---|---|
| Inputs | RTSP (TCP/UDP), RTMP, SRT*, HTTP/HLS, MPEG-TS over UDP, files, capture devices (`-i 0`, `dshow://video=NAME`, `v4l2:///dev/video2`), `onvif://` `hik://` `dahua://` `xmeye://` `reolink://` `axis://` `tapo://` `unv://` |
| Decoding | software (multi-threaded), `cuda` (NVDEC/CUVID), `qsv`, `vaapi`*, `videotoolbox`, `d3d11va`, `dxva2`, `rkmpp`/`v4l2m2m` (SBCs) |
| Display | OpenGL shader path (NV12/YUV direct), QPainter fallback, frame-rate cap, zoom, fullscreen, focus-coloured borders |
| Recording | fragmented MP4 segments aligned to the clock, camera-clock timestamps, NVENC/QSV/AMF/VideoToolbox/x264, `-vf` filters, `-r` decimation |
| Streaming | RTMP (YouTube, Twitch), HLS (YouTube HLS ingest incl. HEVC), SRT*, MPEG-TS; silent-audio synthesis |
| Audio | PortAudio playback with device selection and resampling fallback |
| Discovery | ONVIF (port probing, vendor-agnostic SOAP), Hikvision ISAPI, Dahua CGI, XMeye DVRIP, Reolink HTTP API, Axis VAPIX; Tapo and Uniview paths; recorder channels |
| Operations | `nova` launcher (`start`, `record`, `run`, `stop`, `restart`, `status`, `layout`) with a git-ignored camera list and a control channel per camera, layouts (save / apply to running windows / export to Wayland window rules), auto-reconnect, bounded I/O timeouts, clean SIGINT/SIGTERM shutdown, masked passwords, private files, pre-commit secret guard |

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
| `control.py` | the camera's control channel (`nova stop / status / layout apply` talk to a running camera through it) |
| `doctor.py` | `nova doctor`: versions, GPU and OpenGL driver, the FFmpeg build, audio devices, a masked summary of the configuration, checks |
| `videoworker.py` | connect / decode thread, HW decoder selection, URI resolution, frame fan-out |
| `view.py`, `novagl.py` | frameless window, OSD, geometry, zoom; OpenGL shader renderer |
| `avwriter.py` | encoder selection, timestamps, `-vf` graph, muxing to files and live targets |
| `recorder.py`, `streamer.py`, `recworker.py` | segment rotation, stream lifecycle, encoder threads |
| `audioworker.py`, `audioplayer.py` | PortAudio playback |
| `auth.py` | ONVIF / Hikvision / Dahua / XMeye / Reolink / Axis / Tapo / Uniview URL resolution |
| `ffparser.py`, `ffopts_tables.py`, `tools/gen_ffopts.py` | `--ffmpeg` string → input / decoder / encoder / muxer options, routed by FFmpeg's own option tables (generated from `ffmpeg -h`, the codec tables read from the PyAV build) |
| `util.py`, `logsync.py` | platform checks, password/stream-key masking (logs, launcher output, the pre-commit hook), log setup |
| `dispframe.py`, `zoomer.py`, `mousehandler.py`, `keyhandler.py`, `helper.py`, `player.py`, `mytools.py`, `fonts.py`, `codec_names.py` | display frame container, zoom math, input handling, help overlay (`docs/gen_keys.py` builds the manual's tables from it), the Player base of recorder / streamer / audio, timing helpers, fonts, codec display names |

## Development notes

- Any file FFmpeg can open works as `-i`, so a test clip replaces a camera. For realistic timing feed it at real time: `ffmpeg -re -stream_loop -1 -i clip.mp4 -c copy -f mpegts udp://127.0.0.1:5004` and `-i udp://127.0.0.1:5004`.
- GUI tests without a desktop: `Xvfb :99 -screen 0 1280x800x24 &` then `DISPLAY=:99 python nova.py --opengl …`. `--headless` runs the full decode/record/stream pipeline with no window.
- After editing the key descriptions in `helper.py` or the command-line options in `novaargs.py`, run `python docs/gen_keys.py` to refresh the manual (`--check` only verifies; CI runs it). `python tools/gen_ffopts.py --ffmpeg PATH --source FFMPEG_SRC > ffopts_tables.py` regenerates the `--ffmpeg` routing tables from another FFmpeg.
- Tests: `pip install -r requirements-dev.txt` then `pytest`. They need no camera and no GPU: option parsing, URL masking, the launcher's logic, the timestamp mapper, and a headless decode → libx264 → fragmented-MP4 recording of a generated clip. GitHub Actions runs them on Linux for every push and on Windows and macOS weekly (`.github/workflows/`). `pytest --cov=. --cov-report=term-missing` shows what they leave out (about 30 %: the Qt window, the OpenGL renderer, the hardware decoders and the process entry points, which are checked by hand on a desktop).
- `git config core.hooksPath .githooks` enables the pre-commit guard against committed credentials.

## Built with Claude

Nova's design and its original code are Andy (Wonyoung) Lee's: it began in June 2023 as an OpenCV + PySide2 viewer (the first `nova.py` is dated 2023-07-10), moved to PySide6 in 2024, and the PyAV-based sources of 2026-01-30 are what this repository started from. Since 2026-10-11 the work on this repository has been done together with Claude, Anthropic's AI model (Claude Opus 5.5, then Claude Fable 5.1), in Claude Code sessions on the author's machine: as of 2026-10-14, 93 of the 97 commits carry a `Co-Authored-By: Claude` line. The author set the goals and made the design decisions; Claude read the code, proposed, measured and implemented; everything went through the test suite, CI on three platforms and, where it mattered, the author's own camera wall.

What Claude did in that time:

- Three full reviews of the code and the fixes that followed: crashes and silent stops, secrets kept out of command lines, logs and files, the launcher's control channel, the recorder's recovery and retries.
- `--ffmpeg` option routing rebuilt from FFmpeg's own option tables; the brand URL shortcuts checked against the makers' documentation and real devices; FFmpeg's log levels matched to Nova's; a pass over every input Nova parses.
- The test suite, `nova doctor`, the issue templates, the manual in `docs/`, the changelog, and the measurements quoted in this README.

What Claude could not verify is said so in the documentation: the Windows and macOS windows beyond what CI's tests reach, and camera brands other than the ones on the author's network.

## License

Nova is released under the [BSD Zero Clause License](LICENSE) (0BSD): use, copy, modify and redistribute it for any purpose, with or without attribution. © 2026 Andy (Wonyoung) Lee, ANDYwyLEE@gmail.com.

The licence covers Nova's own source. Its dependencies keep their own terms: PySide6/Qt (LGPL), PyAV, PyOpenGL, NumPy (BSD), PyAudio (MIT). The FFmpeg bundled in the PyPI `av` wheel is built with GPL components (libx264, libx265), so if you ever redistribute Nova *together with* that wheel as a packaged binary, the GPL applies to that bundle.
