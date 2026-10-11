# Changelog

## 2026-10-11 — `nova` launcher

### Added
- `nova` (Linux, macOS) and `nova.bat` (Windows): one entry point for the camera list — `nova start | record | run | stop | status | list`, `nova -h`. Both use the project venv automatically; `cams.py` is the implementation.
- `nova record`: headless recording from the first frame (`--headless -r`). `start --headless` without `-r` or `-t` is refused, because such a camera could neither be seen nor controlled.
- `start` / `record` report a camera that exits within 1.5 s, with the end of its log, instead of calling it started; `status` shows each process's mode (window / headless, rec, stream).

### Changed
- `cams.py` finds `venv/bin/python` (or `venv\Scripts\python.exe`) next to it when `[defaults] python` is not set, so it also works when started with the system interpreter.
- `nova run`: Ctrl-C waits for nova.py to save its state instead of killing it after 0.25 s (`subprocess.call` behaviour).
- `.nova-pids.json` entries carry the mode; old files are still read.

### Removed
- `s.sh`, `startall.sh`, `stopall.sh`, `s.bat`, `startall.bat`, `stopall.bat`: replaced by `nova run`, `nova start`, `nova stop`.

## 2026-10-11 — documentation split

- README.md is now the landing page; the manual lives in `docs/`: `install.md`, `user-guide.md`, `recording.md`, `streaming.md`, `troubleshooting.md` (plus the existing `ffmpeg-options.md` and `custom-ffmpeg.md`).
- `docs/gen_keys.py` regenerates the option / key / mouse tables in the user guide from `helper.py`, so the in-app help (**H**) and the manual cannot drift apart. The help table gained the missing mouse actions (double-click, control strip).

## 2026-10-11 — camera list and credentials

### Added
- `cams.py`: cross-platform launcher (`list`, `start`, `stop`, `status`, `run`) driven by `cameras.ini`, which is git-ignored and holds the camera URLs with their passwords. `cameras.example.ini` is the committed template with placeholder credentials. Per-camera logs in `logs/`; `stop` sends Ctrl-C semantics (SIGINT, Ctrl-Break on Windows) so state is saved.
- `.githooks/pre-commit`: refuses commits containing real-looking camera passwords or YouTube stream keys (`git config core.hooksPath .githooks`).

### Changed
- `s.sh`, `startall.sh`, `stopall.sh`, `s.bat`, `startall.bat`, `stopall.bat` are now credential-free wrappers around `cams.py`; `s.ps1` removed.
- `nova.py` handles SIGBREAK (Windows Ctrl-Break) like SIGINT.

## 2026-10-11 — code review follow-up

### Fixed
- Recording silently produced empty files (`AVRecorder.write` referenced a missing attribute; the worker swallowed the exception).
- `--accel vaapi / videotoolbox / d3d11va / dxva2` decoded in single-threaded software while logging a hardware "success"; the hwaccel context from `av.open` is now used, and the software fallback is multi-threaded and reported honestly.
- XMeye URL resolution crashed when the RTSP port was not 554; ONVIF parsing depended on vendor-specific XML namespace prefixes.
- Audio playback never opened on Linux systems without PipeWire/PulseAudio (`NameError`).
- `--grid-size` and `-n` had no effect; Windows launch scripts passed `-u` (unknown option) and quoted `%*`.
- `--ffmpeg "-b:v 4M"` and other stream-specifier options were ignored; muxer options never reached the container.
- Window geometry/zoom were lost when quitting with Ctrl-C or `stopall.sh`.
- Headless mode could leave a zombie process after a fatal decoder error.
- Multi-monitor desktops with a screen left of / above the primary one.

### Changed
- Timestamps follow the camera clock with wall-clock re-anchoring (`_Timeline`); no A/V drift when the declared frame rate is wrong; outages become gaps.
- Encoder selection follows the output container: RTMP → H.264 only (YouTube), HLS/MPEG-TS → H.264/HEVC, files → the camera's codec family first. Previously `av1_nvenc` was chosen for everything.
- MP4 recordings are fragmented (`+frag_keyframe+empty_moov`); keyframe interval defaults to 2 s; HEVC gets the `hvc1` tag.
- Live targets without camera audio receive a silent AAC track.
- Audio packets are held until the first video packet to keep the muxer's timestamp handling correct with look-ahead encoders.
- ONVIF/XMeye/Hik/Dahua resolution moved to the decoder thread; the URI cache no longer stores passwords; logs mask credentials.
- Encoder/RTMP connection setup runs on the worker thread (no GUI stalls).
- README rewritten per OS; `requirements.txt` pinned to the working versions.

### Added
- `--ffmpeg`: `-vf` (libavfilter chain), `-r`, `-s`, `-pix_fmt`, `-ar`, `-ac`, `-metadata`, `-an`, `-vtag`; warnings for options the codec ignored, for encoders that reject the given options, and for ffmpeg-CLI-only options.
- Capture devices: `-i 0`, `dshow://video=NAME`, `v4l2:///dev/videoN`, `avfoundation://N`.
- OpenGL: direct YUV420P upload for software-decoded streams, OSD texture caching, BT.709 for HD, 3.3 core profile requested before the window is shown.
- Bounded I/O (`av.open` timeout) so shutdown never hangs; graceful SIGINT/SIGTERM.
- `docs/ffmpeg-options.md`, `docs/custom-ffmpeg.md`, `CHANGELOG.md`.
- `LICENSE`: BSD Zero Clause (0BSD).

## 2026-01-30 — baseline

Sources as they were before the review (commit `c1e0e84`).
