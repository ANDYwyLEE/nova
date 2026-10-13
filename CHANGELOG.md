# Changelog

## 2026-10-13 — help overlay left the window dark

### Fixed
- Pressing `H` / `F1` (or clicking the help icon) a second time removed the help text but left its dimming layer behind, so the window stayed at about a third of its brightness until restart. `Helper.off()` emits `None` through a `Signal(str)`, which PySide6 delivers as `""`; `set_hlp` now treats both as "help off" and repaints at once. Found on a camera wall where one window "looked darker than before"; the burnt-in camera timestamp measured 93/255 against 250/255 in a fresh window.

## 2026-10-13 — Wayland checked, XWayland by default

### Added
- `nova layout export NAME hyprlang`: `windowrule { }` blocks for Hyprland 0.53–0.54 (the version Ubuntu 26.04 ships; `hyprland` stays the Lua form for 0.55+). 0.53 verified with `Hyprland --verify-config` and a nested instance; the Lua form follows the current wiki.

### Changed
- On a Wayland session Nova runs under XWayland by default (`QT_QPA_PLATFORM=xcb` when `WAYLAND_DISPLAY` and `DISPLAY` are set): GNOME and KDE honour X11 positions, so placement works there with no setting at all; `QT_QPA_PLATFORM=wayland` still gives a native window. Measured with nested compositors on Ubuntu 26.04 (GNOME Shell 50, KWin 6.6, sway 1.11, Hyprland 0.53): a native window is centred (GNOME, KDE) or tiled (sway, Hyprland) whatever `--geometry` says, only the size is honoured; under XWayland GNOME and KDE honour positions and moves, sway and Hyprland tile; the exported rules place XWayland and native windows exactly on sway, KWin and Hyprland.

### Fixed
- `--opengl` in a native Wayland window failed with PyOpenGL's "no valid context": PyOpenGL defaults to GLX. Nova sets `PYOPENGL_PLATFORM=egl` there.

### Known
- NVIDIA driver 595, native Wayland: a frameless OpenGL window (Nova's) stays invisible, while decorated Qt GL windows and the Mesa drivers are fine. XWayland, the default, is not affected.

## 2026-10-12 — v1.0: window titles, restart, Wayland rules

### Added
- Every window is titled `Nova - NAME` (the `-n` name, else the camera's host), so the taskbar and Alt-Tab tell the cameras apart. Qt reports the app id `nova` on Wayland.
- `nova restart [ID ...]`: stop and start again as launched (`record` stays headless, a window keeps its layout, `--layout NAME` switches). `nova status` shows each camera's uptime.
- `nova layout export NAME sway|kwin|hyprland`: window rules carrying the layout's positions for compositors that do not let a window place itself — sway/i3 `for_window` lines, a `.kwinrule` file for KDE's Window Rules import, `hl.window_rule` calls for Hyprland ≥ 0.55. They match the window title.

### Fixed
- An 8 kHz mono camera made FFmpeg's AAC encoder log `Too many bits 8832.000000 > 6144 per frame requested, clamping to max` at every recording start: its default 69 kbit/s is above the encoder's limit of 6 × sample rate × channels bit/s. Nova applies that limit itself (48 kbit/s at 8 kHz mono) to the default and to a too-high `-b:a`, with one INFO line.

## 2026-10-11 — tests and CI

### Added
- `tests/` (pytest, no camera or GPU needed): `mask_url` incl. stream keys, `FFmpegParser` routing, the launcher's ini parsing / id splitting / GPU dealing / layouts / pid file, `_Timeline`, module imports, `--help`, and an end-to-end headless recording of a PyAV-generated clip (decode → libx264 → 4 s segments → clean SIGINT exit). `requirements-dev.txt`, `pytest.ini`.
- GitHub Actions: `ci.yml` runs the suite and `docs/gen_keys.py --check` on Linux (Python 3.14) for every push; `cross-platform.yml` runs it on Windows (3.13, PyAudio has no 3.14 wheel) and macOS (Apple Silicon, 3.14) weekly, by hand, and when the requirements change.

## 2026-10-11 — stop while a camera is down

### Fixed
- `nova stop` / Ctrl-C could take up to `NOVA_TIMEOUT` (10 s) when a camera was unreachable: `av.open()` cannot be interrupted before its timeout. The worker now probes the host with a plain TCP connect in 0.5 s slices that honour the stop flag, shows `Unreachable: host:port` in the window and logs the state change once; stop is immediate in that state.
- `Clock.wake()` could be lost when it raced with `sleep()` (the retry pause then ran its full length).

## 2026-10-11 — layouts

### Added
- Layouts: `nova layout save NAME` records which cameras are open and where (`layouts/NAME.json`); `nova start --layout NAME` opens exactly those cameras at those places; `nova run ID --layout NAME` for one camera. `nova.py --geometry x,y,w,h` and `--zoom rx,ry,rw,rh` set the initial placement and zoom directly.

### Changed
- Windows persist geometry and zoom within 2 s of a change, not only at exit, so a crash loses nothing and `layout save` sees the wall as it is.
- Qt no longer sees Nova's command line (`QGuiApplication([argv[0]])`): on X11 it consumed `--geometry` as an X geometry string and overrode the window width. Use `QT_*` environment variables (`[env]` in `cameras.ini`) for Qt settings.

## 2026-10-11 — `nova` launcher

### Added
- `nova` (Linux, macOS) and `nova.bat` (Windows): one entry point for the camera list — `nova start | record | run | stop | status | list`, `nova -h`. Both use the project venv automatically; `cams.py` is the implementation.
- `nova record`: headless recording from the first frame (`--headless -r`). `start --headless` without `-r` or `-t` is refused, because such a camera could neither be seen nor controlled.
- `start` / `record` report a camera that exits within 1.5 s, with the end of its log, instead of calling it started; `status` shows each process's mode (window / headless, rec, stream).
- `--gpu N` (default: `NOVA_GPU_DEVICES`). With a GPU list in `[env]`, `nova start` / `run` deal the cameras over the GPUs in file order (deterministic, balanced) instead of each process picking one at random; `nova list` and `status` show the GPU.

### Fixed
- Passwords could appear in error logs: PyAV embeds the full URL in its exception text (`open exception: ... 'rtsp://user:pw@...'`). A logging filter now masks URL credentials and stream keys (`live2/KEY`, `?cid=`, Twitch `live_`) in every log record, whichever module wrote it.

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
