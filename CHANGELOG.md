# Changelog

## 2026-10-14 — brand shortcuts: the RTSP port asked from the right place, recorder channels, four more brands

A check of every vendor URL format against the makers' documentation and the reference clients (Home Assistant's `hikvision_next`, `reolink_aio` and `icsee-ptz`, `python-amcrest`).

### Fixed
- `hik://`, `dahua://` and `xmeye://` never learned the device's RTSP port and always produced 554: Hikvision's channel list carries no port (it is the RTSP entry of `/ISAPI/Security/adminAccesses`), Dahua's config section is `RTSP` (not `Rtsp`), XMeye keeps it in `NetWork.RTSP` (`Server.Port`), not `NetWork.NetCommon`. Each is now read from the right place and checked as before (a reported port must be a number); a device that reports RTSP switched off (Hikvision `enabled`, Reolink `rtspEnable`, XMeye `IsServer`) gets a warning naming the setting.
- `hik://` checks that the stream exists in the device's channel list and lists the ones it has; `xmeye://` builds the path the device itself shows (`…stream=0.sdp?real_stream`).
- A malformed shortcut URI is explained in the log with the expected form.

### Added
- **Recorder channels and the third stream.** `scheme://user:pw@host[:port][/CHANNEL][/STREAM]`: `hik://admin:pw@nvr/3/sub` is channel 3, sub stream; `STREAM` is `0|main`, `1|sub` or `2|third` (Hikvision, Dahua, Uniview). An ONVIF profile is picked with `/N`.
- **`reolink://`** (HTTP API: login, ports, channel count, the device's own RTSP URL via `GetRtspUrl`, logout; HTTPS first, plain HTTP only with a warning; older firmware gets `h264Preview_…`/`h265Preview_…` from `GetEnc`), **`axis://`** (VAPIX: the RTSP port, `?camera=N` for a written channel), **`tapo://`** and **`unv://`** (the makers' documented paths, nothing asked of the device) and the OEM aliases `amcrest://`, `annke://`, `hilook://`. User guide: a "Camera brands" table.

## 2026-10-14 — second full review: crashes, silent stops, secrets, the launcher

A review of every module (five reviewers in parallel, each finding reproduced or confirmed in the code) and the fixes, tested against the old code where a reproduction was possible.

### Added
- **`nova doctor`** (`python doctor.py` without the launcher): one screen with what this machine runs Nova with: Nova, Python, Qt and PyAV versions, the FFmpeg build inside PyAV and what it was built with (wheel or source), the desktop session and the OpenGL driver Nova's windows get (probed in a child process, so a broken driver cannot take the report with it), hardware decoders and encoders, GPUs, audio devices, a summary of `cameras.ini` with passwords and stream keys masked, settings and recording folders with free space, the last warnings in `logs/`, and checks with a verdict (software OpenGL, NVIDIA driver too old for the wheel's NVENC, `cameras.ini` readable by others, `--accel` without a matching decoder, low disk space, PyAV not the pinned version). Everything printed goes through the secret masker once more.
- Issue templates (`.github/ISSUE_TEMPLATE/`: bug, camera does not connect, feature request) that ask for the `nova doctor` output, a `-l DEBUG` log and a confirmation that nothing secret was pasted; a pull request template with the test/docs/changelog checklist.
- **Control channel per camera.** A launched camera listens on a socket only its user can open (`$XDG_RUNTIME_DIR/nova/ID.sock`, a named pipe on Windows). `nova stop` uses it (signals remain the fallback for cameras started before), `nova status` shows what each camera does now (`video 1920x1080, record on, stream waiting`, or the overlay message while it has no picture), and **`nova layout apply NAME`** moves open windows to a layout's places and zoom without restarting them.
- Recording restarts by itself after a failure (disk full, unwritable folder, encoder failure): 5, 10, 20, 40 s, then every 60 s, gray camera icon meanwhile, `record waiting` in `nova status`. Before, one failure switched recording off for good and nothing showed it.
- A crash leftover (`<name>_output.mp4` after power loss or `kill -9`) is kept as `YYYYMMDD_HHMM_recovered.mp4`; the next start used to overwrite it. One process records a camera folder at a time (`.nova-recording.lock`).

### Changed
- **`--ffmpeg` routing rebuilt on FFmpeg's own option tables.** Every `-key value` now goes wherever FFmpeg defines an option of that name: the generic codec options by their own decoding/encoding and video/audio flags, a decoder's or encoder's private options, the generic format options by their demuxing/muxing flag, demuxer, muxer, input-protocol and hardware-device options (`ffopts_tables.py`, generated from `ffmpeg -h full` of FFmpeg 9.0.2 by `tools/gen_ffopts.py`; the codec tables are read from the PyAV build at run time, so a custom FFmpeg brings its own). The hand-written tables they replace came from a summary of the `ffmpeg` program's options and sent `-threads`, `-flags`, `-strict` to one side only, took program options (`-hwaccel_device`, `-qsv_device`, `-vsync`, `-cpuflags`, `-max_alloc`, …) and removed ones (`-stimeout`) for library options, and listed names that exist nowhere (`-cuvid_device`, `-bitrate`, `-thread_count`). An option defined on several sides reaches every one of them, as in `ffmpeg`; when a codec opens, options of *other* codecs (`-cq` for libx264, `-resize` for a software decoder) are left out quietly so that the "ignored unknown option" report stays meaningful. A bare `-i` splits input from output options like an `ffmpeg` command line (`-f`, `-s`, `-r`, `-pix_fmt` before it are the input format and capture-device size, rate and pixel format). Program options are reported with a hint (`-t … use --record-interval`), `-stimeout`/`-vsync` with what replaced them, and unknown names with where they were sent. The audio encoder now gets audio-only options (`-ab`, `-cutoff`, `-aac_*`, libopus's) without `:a`. `docs/ffmpeg-options.md` rewritten and re-measured with FFmpeg 9.0.2.
- The writers no longer hand `qsv_device`, `vaapi_device` and `hwaccel_device` to the qsv/vaapi/amf/mf encoders: those are `ffmpeg` program options (silently dropped by the encoder); only NVENC's `gpu` exists. `-hwaccel_device N` selects the device of `--accel` instead of `--gpu`.
- **No secrets on command lines.** A launched camera runs as `nova.py --camera ID --config cameras.ini` and reads URL, name and options itself; its command line, readable by every local user (`ps`), carried the camera password and the YouTube key.
- **Private files.** Nova and the launcher create files readable by their user only (umask 077): recordings, snapshots, logs, layouts, the pid file, Nova.conf on its next write. The launcher warns when cameras.ini is readable by others. `NOVA_UMASK = 022` in `[env]` restores the old behaviour for setups where another account or a service reads `videos/`.
- `--accel cuda`: the container is opened without hardware acceleration and the decoder is chosen after the stream is known, so each camera has one CUDA context instead of two: 796 → 410 MiB of VRAM per camera on an RTX 4090 (≈ 5.7 GB over 15 cameras; measured again with two cameras decoding, recording and streaming on one GPU).
- Segments follow the local clock (and daylight-saving time) instead of UTC: `24h` cut at 01:00/02:00 in Central Europe.
- `nova stop`/`restart` wait 2 × NOVA_TIMEOUT + 5 s by default (25 s with the example config), the longest a camera needs while its stream is stalled, and 30 s more for cameras with a stream target (caught in the stream's connect).
- **Wheel direction.** Pushing the wheel away now zooms in and pulling it toward you zooms out, as in maps, image viewers and games (it was the other way round). On macOS "natural scrolling" is taken into account, so the hand's movement means the same on a trackpad, and a **trackpad pinch** zooms as well. `NOVA_WHEEL_INVERT = 1` reverses the wheel.
- **Zoom and window size are independent.** The zoom is a region of the picture; resizing the window keeps the region and shows it at the new size, and the space a wider or taller window adds shows the picture around the region instead of black bars (the region is never cut; black only where the frame runs out). Narrowing the window back shows exactly the region again. New: **Ctrl-drag** and **Shift+Arrow** move the region inside the window. The wheel, I/O, middle click act on what is shown.
- Held keys repeat only moves, resizes and zoom; a wheel notch zooms 10 % and touchpads by their fraction of a notch (they zoomed 8–15× too fast).

### Fixed — crashes
- A recording or stream whose output could not be started (disk full when a segment begins, a publish refused after connect) **crashed Nova**: PyAV writes the header a second time and segfaults. Reproduced with PyAV alone and with a segment file on /dev/full; now the writer is freed and recording restarts.
- An encoder that passed the probe but does not open at the real size (NVENC session limit, GPU fault) **crashed Nova** at its second frame; the writer now continues with the next encoder, ending at libx264, without losing a frame. An audio encoder that cannot open leaves the audio track out.
- A decoder thread still blocked at exit made Qt abort the process (SIGABRT, a crash report); Nova now leaves without it.
- `-l VERBOSE` failed at start (`Unknown level`).

### Fixed — recordings and streams
- Video errors were reset by every successful audio write: no escalation, a traceback per frame, and the segment was then deleted with its audio. Errors are now counted per stream type, logged once per kind, and end the writer after 60 in a row (so recording restarts).
- The first segment had no audio, and a stream stayed silent, when the writer opened before the camera's first audio frame; a camera's audio format change broke the track (`Frame does not match AudioResampler setup`).
- With recording and streaming both on, the stream lost its video for MJPEG 4:2:2, 10-bit HEVC (`hevc_cuvid`) and odd frame sizes (2 frames instead of 274 in the test).
- `-e hevc_nvenc` turned RTMP streams into HEVC (YouTube: no data); a forced encoder the target cannot carry is now skipped with a warning.
- A 10 h camera outage was sent to a live target as a 33 s burst of silent AAC; no audio device gave ~100 ERROR lines a second; frames dropped by a writer that cannot keep up are now reported; a segment cut by an outage is named by its last frame; a failed rename no longer lets the next segment overwrite the file; the encoder probe lock is per user (another account could stall every probe); a malformed `NOVA_TIMEOUT` no longer stops streams.

### Fixed — cameras and decoding
- A hardware decoder that cannot be used (wrong `--gpu`, GPU out of memory, a stream it cannot decode such as rawvideo webcams or MJPEG 4:2:2) failed the connection every 10 s forever; Nova now decodes in software at once, without reconnecting, and tries hardware again after 5 min (then 10, 20, 40, every 60 min) alongside the software decoder.
- One damaged video or audio packet ended the connection; it is skipped. Video that stops while audio flows, or a picture that stops, now triggers a reconnect.
- **The zoom was lost at every camera reconnect** and the reset was saved (also into layouts): the decoder reported the size again, the window rebuilt its zoom and saved `0,0,1,1` while the size read 0. The decoder now keeps the size across reconnects and the window keeps its zoom either way; a restored zoom no longer drifts a pixel per restart.
- A password with an unencoded `/ ? #` crashed the camera worker (and printed part of the password); a dual-stack host with a dead IPv6 address was reported unreachable; slow DNS held up stop. A frame rate taken from the RTP clock gave writers a keyframe interval of 180000. Repeated open errors (401 …) are logged once.

### Fixed — windows
- Quitting while fullscreen or maximized saved the screen-sized rectangle as the window's place.
- A place on a monitor that is not connected opened the window off-screen; when a monitor drops out (unplugged, DisplayPort asleep) the window manager's moves were saved. Windows keep their places and return when the monitor is back.
- Double-click: leaving fullscreen could snap to the fullscreen rectangle; on a control-strip icon it toggled twice and switched fullscreen. Wheel and middle-click zoom showed only with the next frame.
- OpenGL errors logged a traceback per frame and left the window black; they are reported once a minute, the GL state is rebuilt once, then the window says "Rendering failed (OpenGL)". OSD layers drew in a different order in the two renderers, were blurry on HiDPI, and the border turned orange on unfocused windows after a resize. Where the platform cannot move or resize a window (macOS border resize), Nova does it.

### Fixed — launcher
- A stale pid entry (after a reboot or crash) made `nova stop` interrupt whatever process had that pid and `start` skip the camera; an entry now counts only while that process is a Nova camera. Two parallel commands launched cameras twice and lost pids; commands that start or stop cameras take turns, the pid file is replaced atomically, and an unreadable one stops the command.
- `restart` stopped cameras and then failed on a missing layout; layouts are checked first. cameras.ini: keys are case-insensitive (`Enabled = no` was ignored) and unknown keys reported, a BOM is accepted, parse errors no longer echo a URL, ids that cannot name a log file are skipped. Logs rotate at 50 MB per start. `./nova` works through a symlink; Windows cameras run without a console (closing the terminal killed them) and keep backslashes in `opts` paths; sway/KWin exports survive quotes and brackets in names; headless cameras always use the offscreen platform.

### Fixed — secrets and the camera shortcuts
- Passwords reached logs: the **P** key printed the full URL into `logs/ID.log`; `nova list` printed stream keys in `opts`; masking missed an `@` or `/` inside a password, XM `_password=`, `psw=`, SRT `passphrase`/`streamid`, RTMP keys other than YouTube/Twitch, dict and argv forms, and tracebacks (logged and uncaught). One pattern table now serves the logs, `nova list` and the pre-commit hook, which also checks moved files, copes with spaces in names and refuses cameras*.ini, logs and layouts; CI runs the same scan over all tracked files.
- `onvif://`, `hik://`, `dahua://`, `xmeye://`: the device's answer decided where the password was sent (a port of `554@other-host` redirected it); ports must be 1..65535 and stream URIs rtsp/rtsps on the camera's own address. Hikvision no longer falls back to HTTP Basic over plain http. `&amp;` in ONVIF URIs is decoded (substreams were ignored), passwords may contain `@`, percent-encoded credentials are decoded, fallback URLs are not cached, the ONVIF port scan takes 1.5 s instead of up to 37 s, and TRACE logs no longer show the WS-Security digest.

### Docs
- The command-line option table in the user guide and the H overlay are generated from `nova.py`'s own parser (`novaargs.py`), so they cannot drift from `--help` again; they had: `--opengl` described as hardware acceleration, `--accel` missing backends, `-f`/`-e`/`-n`/`--ffmpeg` outdated, `--camera`/`--config` missing. A test checks that every option, environment variable and launcher command is in the manual.
- Corrected: the stream starts with `-t` (T stops it), `-skip_frame nokey` also thins recordings, SRT options go in the URL, PgUp shrinks, snapshot names, NOVA_TIMEOUT defaults, Python 3.12+, encoder order, quoted log messages checked against the code. `python-dvr` is listed for `xmeye://`.

## 2026-10-13 — native Wayland windows on NVIDIA show the picture

### Fixed
- `--opengl` in a native Wayland window (`QT_QPA_PLATFORM=wayland`) stayed empty with the NVIDIA driver. Cause: Qt's Wayland platform first makes a new desktop-OpenGL context current without a surface (to read back its format), and NVIDIA's EGL then keeps the window's draw buffer at `GL_NONE` once the window surface is bound, so every draw call was discarded and the compositor showed the transparent, never-drawn buffer. Mesa, OpenGL ES contexts and GLX all start at `GL_BACK`, and windows with client-side decorations render into an FBO first, which is why only Nova's frameless window was hit. `initializeGL` now resets the draw buffer to `GL_BACK` when it finds `GL_NONE`. Reproduced without Qt in a 100-line EGL program (surfaceless-first `eglMakeCurrent` → `GL_DRAW_BUFFER` 0 on NVIDIA 595.99, `GL_BACK` on Mesa 26.0), and verified with Nova in a nested sway on an RTX 4090: empty before, the picture after; X11 unchanged.

## 2026-10-13 — live streams reconnect

### Added
- A network stream (RTMP, SRT, HLS, UDP) that fails while switched on is reopened by itself: after 5, 10, 20, 40 s, then every 60 s until it connects or **T** switches it off; a stream that ran for a minute starts the series again. While it waits the LIVE icon stays visible but gray, and the log says `[AVStreamer] reconnecting in N s (attempt K)`. Previously any failure switched the stream off for good, which ends an overnight stream at the first network blip. File targets are not reopened (that would overwrite the file). Tested against a local RTMP server killed and restarted, and one paused mid-stream: the stream came back on the first attempt after the server did.

### Fixed
- An output that cannot be opened (connection refused, wrong key, DNS) is given up at once: PyAV retries the open on every later packet, so a refused stream used to reconnect up to 60 times a second before the worker gave up.

## 2026-10-13 — a stalled stream no longer hangs Nova

### Fixed
- A live stream whose connection stalled (ingest server or uplink stops accepting data, no reset) blocked the streamer forever, and stopping it (`T`, closing the window, `nova stop`) hung with it: PyAV opens the output connection with `avio_open2(…, NULL)`, so the `rw_timeout` Nova passed never reached the RTMP protocol. Network targets now get PyAV's own deadline, `av.open(timeout=(30 s, NOVA_TIMEOUT))`: a write that makes no progress for `NOVA_TIMEOUT` ends the stream with `[AVStreamer] write timed out, giving up` (no 60 retries, the half-written packet has broken the stream anyway). Measured against a local RTMP server paused mid-stream: before, no error and a SIGTERM still pending after 30 s; now the error 13 s after the stall (≈3 s to fill the socket buffer + 10 s) and a SIGTERM during the stall exits in 20 s.
- The worker reports a failure to the GUI only after the writer is closed, so closing a dead connection no longer freezes the window.
- `Some options were not used: {'rw_timeout': …, 'tcp_nodelay': '1', 'rtmp_live': 'live'}` at every stream start: those protocol options are no longer passed (they never had an effect). HLS gets its upload timeout through the muxer's own `timeout` option. docs/ffmpeg-options.md no longer lists `rtmp_*` / SRT options as settable through `--ffmpeg`; SRT takes them in the URL.

### Known
- FFmpeg's RTMP client reads at most one server message per 10 packets it sends (`rtmp_flush_interval`, another protocol option PyAV cannot pass). Against YouTube the unread acknowledgements pile up in the socket's receive queue (about 1 KB a minute at 9.6 Mbit/s); being watched in an overnight run.

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
