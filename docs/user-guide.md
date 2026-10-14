# User guide

- [The camera list: cameras.ini](#the-camera-list-camerasini)
- [The launcher: nova](#the-launcher-nova)
- [Layouts](#layouts)
- [Command-line options](#command-line-options)
- [The window](#the-window)
- [Keyboard](#keyboard)
- [Mouse](#mouse)
- [Zoom](#zoom)
- [Audio](#audio)
- [Snapshots](#snapshots)
- [Where settings are stored](#where-settings-are-stored)
- [Environment variables](#environment-variables)
- [Logs](#logs)

## The camera list: cameras.ini

`cameras.ini` is the only file you edit. It is listed in `.gitignore`, so the camera passwords in it never reach the repository; `cameras.example.ini` is the committed template.

```ini
[defaults]
; options given to every camera - the template lists presets per OS / GPU / log level
opts = --opengl --accel cuda -f 30 -b -l CRITICAL
; camera = frontdoor          `nova run` without an id starts this camera
; stagger = 0.5               seconds between starts

[env]
; environment for the camera processes
NOVA_TIMEOUT = 10000000
; NOVA_GPU_DEVICES = 0,1     two GPUs: `nova start` deals the cameras over them in turn

; one section per camera; the section name is the id you type on the command line
[frontdoor]
name = Front door
url = rtsp://admin:password@192.168.1.10:554/stream_0

[yard]
url = rtsp://192.168.1.11:554/user=admin&password=password&channel=1&stream=0
; per-camera options are appended to the defaults
opts = -r --record-interval 1h
; skipped by `start`
enabled = no
```

| Key | Meaning |
|---|---|
| `url` | anything FFmpeg can open: `rtsp://`, `rtmp://`, `srt://`, `http(s)://`, `udp://`, a file; a brand shortcut that finds the RTSP URL for you: `onvif://user:pw@ip[:port][/N]`, `hik://`, `dahua://`, `xmeye://`, `reolink://`, `axis://`, `tapo://`, `unv://` ([Camera brands](#camera-brands)); `0` / `v4l2:///dev/video2` / `dshow://video=NAME` / `avfoundation://0` for capture devices |
| `name` | window title, the folder under `videos/` and the start of snapshot file names (default: the section name; characters other than letters, digits, `.`, `_`, `-` become `_`) |
| `opts` | any `nova.py` options; appended to `[defaults] opts` |
| `enabled` | `no` keeps the camera out of `start` (it can still be started by id) |

Comments are whole lines starting with `;` or `#`. A `;` after a value is part of the value.

## Camera brands

The RTSP path differs by maker and the port by setup. For the brands below, write a shortcut instead of an `rtsp://` URL: Nova asks the device for its port and stream, or builds the URL from the maker's documented form.

```
scheme://user:password@host[:port][/CHANNEL][/STREAM]
```

`CHANNEL` is the recorder channel (1 = the first; leave it out for a camera), `STREAM` is `0`/`main`, `1`/`sub` or `2`/`third` (default: main). `hik://admin:pw@nvr/3/sub` is the sub stream of channel 3 of a Hikvision NVR; `dahua://admin:pw@cam/1` the sub stream of a camera.

| Shortcut | Also for | What Nova asks, and where | RTSP URL it builds |
|---|---|---|---|
| `onvif://` | any ONVIF camera | the ONVIF service on `:port` (25 common ports probed when none is given): the media profiles and the stream URI | the device's own answer; `/N` picks the N-th profile (0 = the first) |
| `hik://` | Annke (`annke://`), HiLook (`hilook://`), HiWatch | ISAPI on `:80` (HTTP Digest): the channel list and the RTSP port | `/Streaming/Channels/<CHANNEL><1\|2\|3>` |
| `dahua://` | Amcrest (`amcrest://`), EmpireTech and other Dahua OEM lines | the CGI on `:80` (HTTP Digest): the RTSP port | `/cam/realmonitor?channel=<CHANNEL>&subtype=<0\|1\|2>` |
| `xmeye://` | cameras and DVRs set up with the XMeye, ICSee or VMS app (XiongMai firmware) | DVRIP on `:34567`: the channel count, the RTSP port and whether the RTSP server is on | `/user=…&password=…&channel=<CHANNEL>&stream=<0\|1>.sdp?real_stream` |
| `reolink://` | Reolink cameras and NVRs (battery models have no API) | the HTTP API, HTTPS `:443` first, then HTTP `:80` with a warning: login, the ports (and whether RTSP is on), the channel count, the device's own stream URL | the device's answer; older firmware `/h264Preview_<CHANNEL>_<main\|sub>` (`h265` when the stream is H.265) |
| `axis://` | Axis | VAPIX on `:80` (HTTP Digest): the RTSP port | `/axis-media/media.amp` (`?camera=<CHANNEL>` when a channel is written; no numbered sub stream: write a stream profile into an `rtsp://` URL yourself) |
| `tapo://` | TP-Link Tapo (the camera account from the Tapo app, not the cloud login) | nothing: `:port` is the RTSP port | `/stream1` or `/stream2` |
| `unv://` | Uniview | nothing: `:port` is the RTSP port | camera `/media/video<1\|2\|3>`; with a channel written, recorder `/unicast/c<CHANNEL>/s<0\|1\|2>/live` |

What a device answers is checked before the password is attached: the stream must be `rtsp`/`rtsps` on the device's own address with a valid port, a reported port must be a number (never `554@other-host`), and the password only ever goes to the address you wrote (Digest for HTTP, never Basic). A device that reports RTSP switched off gets a warning in the log naming the setting to turn on. For other brands, the maker's manual or the camera's web page gives the RTSP URL, `onvif://` works for most, and `-l DEBUG` shows every answer with credentials masked.

## The launcher: nova

`./nova` on Linux and macOS, `nova` on Windows (`nova.bat`). Both use the project `venv` automatically, so nothing needs activating; `cams.py` is the implementation.

```
nova start  [ID ...] [options]       a window per camera: every enabled camera, or the given ids
nova record [ID ...] [options]       the same without windows, recording from the first frame
nova run    [ID]     [options]       one camera in this terminal: log on screen, Ctrl-C quits
nova stop   [ID ...] [--timeout S]   stop cameras started by start/record (saves state like Ctrl-C)
nova restart [ID ...] [options]      stop, then start again as launched (record stays record, layout stays)
nova status                          launched cameras: running or exited, uptime, and what each is doing now
nova list                            cameras, enabled state and options (passwords masked)
nova doctor                          versions, drivers, the FFmpeg build, cameras.ini summary: paste it into a bug report
nova -h                              help; `python nova.py --help` lists the camera options
nova -c FILE <command>               use FILE instead of cameras.ini
```

- Words that do not start with `-` are camera ids; everything from the first option on is passed to `nova.py`: `nova start frontdoor -r` shows the window and records, `nova record yard -t rtmp://…` records without a window and streams.
- Options a camera should always get belong in its `cameras.ini` section (`opts = -r`), so a plain `nova start` does the right thing per camera.
- `start` and `record` detach from the terminal, write each camera's output to `logs/ID.log` (moved to `ID.log.1` at a start once it passes 50 MB) and remember the process ids in `.nova-pids.json`. A camera that exits within the first seconds is reported with the end of its log instead of being called "running".
- A launched camera gets only its id on the command line (`nova.py --camera ID --config cameras.ini`) and reads URL, name and options from `cameras.ini` itself: a process's command line is visible to every user of the machine (`ps`), its passwords and stream keys are not.
- Each camera has a **control channel**, a socket only you can open (in `$XDG_RUNTIME_DIR/nova/`; a named pipe on Windows). `stop`, `status` and `layout apply` use it; cameras started by an older Nova are still stopped with a signal.
- Launcher commands that start or stop cameras take turns (a lock), and a pid entry only counts while that process is a Nova camera, so an entry left over from a reboot or crash is ignored instead of stopping whatever program got that pid.
- A camera without a window that neither records nor streams can neither be seen nor controlled, so `start --headless` without `-r` or `-t` is refused; `record` is the shorthand for `--headless -r`.
- `stop` only touches processes it started and waits for them to exit cleanly (window geometry, zoom and open recordings are saved); `--timeout` defaults to 2 × `NOVA_TIMEOUT` + 5 s (25 s with the example config), the longest a camera needs while its stream is stalled, and 30 s more for cameras with a stream target (one caught in the stream's connect).
- `restart` stops the launched cameras (or the given ids) and starts them again the way they were launched: a camera from `record` stays headless, a window keeps its layout (`--layout NAME` switches), and the options from `cameras.ini` apply as always. Options typed after `start` are not remembered, so give them again. A camera that is not running is simply started; a missing layout ends the command before anything is stopped.
- `status` lists every camera the launcher started: pid, running or exited, uptime, mode as launched (window / headless, rec, stream), GPU and layout, and below it what the camera reports right now: video size, recording and stream `on` / `waiting` (reconnecting) / `off`, and the overlay message while it has no picture (`Unreachable: …`).
- `doctor` prints what this machine runs Nova with, on one screen: Nova, Python, Qt and PyAV versions, the FFmpeg build inside PyAV and what it was built with, the desktop session and the OpenGL driver Nova's windows get, hardware decoders and encoders, GPUs, audio devices, a summary of `cameras.ini` (passwords and stream keys masked), where settings and recordings live and how much space is left, the last warnings in `logs/`, and a few checks with a verdict. It is what a bug report needs; `python doctor.py` does the same without the launcher.
- `run` keeps one camera in the foreground: the log appears in the terminal (`nova run ID -l DEBUG` for diagnosis) and Ctrl-C quits. Without an id it runs `[defaults] camera`, or the first enabled camera.

## Layouts

Every window remembers its own place. A layout remembers the whole wall: which cameras are open, where each window sits, how big it is and which part of the picture it shows. Keep as many as you like (`work`, `away`, `one-monitor`, …).

```
nova layout save NAME        write the open cameras' windows to layouts/NAME.json
nova layout list             saved layouts and their cameras
nova start --layout NAME     open exactly the cameras of that layout, where they were saved
nova layout apply NAME       move the open windows to that layout's places now (no restart)
nova run ID --layout NAME    one camera at its place in that layout
nova layout export NAME FORMAT   window rules that place the layout on a Wayland desktop
                                 (FORMAT: sway, kwin, hyprland = Lua config 0.55+, hyprlang = 0.53-0.54)
```

- Arrange the windows, then `nova layout save work`. The cameras can stay open: every window writes its geometry and zoom to Nova's settings within 2 s of a change, and `save` reads them from there. With no camera open, the enabled cameras' last positions are used.
- `nova start --layout away` opens the cameras listed in the layout (`enabled` does not apply) and gives each its saved `--geometry` / `--zoom`. From then on the windows remember their places as usual, so a plain `nova start` afterwards brings the `away` arrangement back; `nova start --layout work` switches again.
- The file is plain JSON, camera id → `"geometry": "x,y,w,h"` and `"zoom": "rx,ry,rw,rh"` (fractions of the picture; `0,0,1,1` is the whole picture). Edit it by hand if you like. `layouts/` is git-ignored, like `cameras.ini`.
- `--geometry x,y,w,h` and `--zoom rx,ry,rw,rh` also work directly on `nova.py`.
- **Wayland**: a compositor does not let a window place itself, so on a Wayland session Nova runs under XWayland by default (it sets `QT_QPA_PLATFORM=xcb` when both `WAYLAND_DISPLAY` and `DISPLAY` are set). GNOME and KDE honour X11 positions, so there everything works as on X11 without further setup. `QT_QPA_PLATFORM = wayland` in `[env]` gives native Wayland windows; then `--geometry` sets only the size and the compositor decides the place (GNOME and KDE centre the window, sway and Hyprland tile it). Checked with nested compositors on Ubuntu 26.04:
  - **GNOME** (Mutter 50): no window rules; XWayland, the default, places and moves windows exactly as on X11.
  - **KDE Plasma** (KWin 6.6): XWayland works the same way; `nova layout export work kwin` additionally gives a `.kwinrule` file (System Settings › Window Management › Window Rules › Import) that places native Wayland windows too.
  - **sway** (i3 too): windows are tiled, XWayland or native, unless a rule floats them: `nova layout export work sway` prints `for_window` lines for `~/.config/sway/config`.
  - **Hyprland**: tiles as well; `nova layout export work hyprlang` for 0.53–0.54 (`windowrule { }` blocks; Ubuntu 26.04 ships 0.53), `hyprland` for 0.55 and later (Lua, `hl.window_rule`). Hyprland's `move` is monitor-local; with several monitors subtract the monitor's position.

  The rules match the window title `Nova - NAME` (the camera's name from `cameras.ini` / `-n`), XWayland and native windows alike, so they keep working across restarts; the positions are the absolute coordinates Nova saved. Under a rule the window still remembers where you drag it, as usual.

## Command-line options

`python nova.py -i URL [options]`. Generated from the in-app help:

<!-- generated:args -->
| Option | Meaning |
|---|---|
| `-i, --input` | Video source: rtsp/rtmp/srt URL; a brand shortcut that finds the RTSP URL (onvif://, hik://, dahua://, xmeye://, reolink://, axis://, tapo://, unv:// followed by user:pw@host[:port][/CHANNEL][/STREAM]); a capture device ('0' = /dev/video0 or avfoundation 0, 'dshow://video=NAME' on Windows). |
| `-n, --name, --camera-name` | Camera name: the window title becomes 'Nova - NAME', recordings go to videos/NAME/ and snapshots are named NAME_<time>.png. |
| `--opengl` | Render with OpenGL shaders (zero-copy NV12/YUV upload; recommended). |
| `--accel` | Hardware decoding backend (cuda, qsv, vaapi, videotoolbox, d3d11va, dxva2, rkmpp, v4l2m2m ...). |
| `--gpu` | GPU index for hardware decoding/encoding (e.g. 1; a list 0,1 picks one at random). Default: NOVA_GPU_DEVICES, else 0. |
| `-f, --fps` | Target display frame rate. Default: 30. |
| `-b, --border` | Enable the colored window border. |
| `-g, --grid-size` | Grid size for window alignment in pixels. Default: 20. |
| `--geometry` | Initial window placement x,y,w,h; overrides the remembered one (nova start --layout uses it). |
| `--zoom` | Initial zoom region as ratios rx,ry,rw,rh of the picture (0,0,1,1 = whole picture). |
| `-a, --audio, --enable-audio` | Enable audio playback automatically on startup. |
| `--headless, --no-gui` | Run in headless mode without showing the visual camera window. |
| `-r, --record, --auto-record` | Start recording automatically on startup. |
| `-e, --encoder` | Explicitly specify video encoder (e.g., h264_nvenc, hevc_nvenc, libx264). |
| `--record-interval` | Recording segment length (e.g. 10m, 1h, 3600s). Default: 30m. |
| `--output-dir` | Base directory for snapshots and recordings. |
| `--snapshot-dir` | Directory for snapshots (overrides --output-dir for snapshots). |
| `--recording-dir` | Directory for recorded videos (overrides --output-dir for recordings). |
| `--ffmpeg` | FFmpeg options as on an ffmpeg command line, e.g. "-rtsp_transport udp -b:v 4M -g 60 -movflags +faststart"; an -i may separate input from output options (docs/ffmpeg-options.md). |
| `-t, --target` | Live stream URL (e.g. YouTube, Twitch) or output path. |
| `-l, --log-level` | Logging level (NAME or NUMBER; e.g., DEBUG, INFO, 5, 10). Default: INFO. |
| `--camera` | Read URL, name and options of camera ID from cameras.ini (what the nova launcher passes). |
| `--config` | The cameras.ini that --camera reads. |
<!-- /generated:args -->

Notes:

- `--opengl` is the recommended renderer. Decoded NV12/YUV planes go straight to textures and the colour conversion runs in a shader; software-decoded streams are uploaded the same way. Without it, QPainter draws RGBA frames converted on the CPU.
- `--accel` picks a dedicated decoder where one exists (`h264_cuvid`, `hevc_qsv`, …) or attaches the platform hwaccel (`vaapi`, `videotoolbox`, `d3d11va`, `dxva2`) to the standard decoder. If neither delivers frames, software decoding takes over and the log says so.
- `-f` caps the *display* rate; decoding always runs at the camera's rate so recordings and streams are complete.
- `-g` is the grid for snapping and keyboard moves. Windows snap when you release the mouse.

## The window

Each camera is a frameless window titled `Nova - NAME` (its name in `cameras.ini`, or the one given with `-n`). The border colour (with `-b`) shows focus: orange = focused, green = not focused. The control strip appears when the mouse enters the top-left corner:

![Control strip](images/nova-window.jpg)

| Icon | Meaning |
|---|---|
| LIVE | streaming to the `-t` target (shown only when a target is configured); gray while it waits to reconnect |
| camera | recording; gray while it waits to retry after a failure |
| speaker | audio playback |
| ? | help overlay (same as **H**) |

Status text in the window ("Connecting…", "Failed: …", "Stream error: …") comes from the decoder thread; Nova reconnects on its own every few seconds. "Rendering failed (OpenGL)" means the OpenGL renderer gave up after rebuilding its state once (the log has the error): `nova restart ID`, or run that camera without `--opengl`.

![Help overlay](images/nova-help.png)

## Keyboard

<!-- generated:keys -->
| Key | Action |
|---|---|
| `H / F1` | Show or hide the help/controls overlay. |
| `F / F11` | Toggle fullscreen mode. |
| `A / F5` | Enable or disable audio playback. |
| `R / F7` | Toggle video recording to disk. |
| `T / F8` | Toggle transmitting live stream (e.g. YouTube, Twitch). |
| `S` | Save a snapshot to disk. |
| `B` | Toggle the window border on or off. |
| `P` | Print the stream URL (the password only to a terminal, masked in a log file). |
| `ESC` | Reset the zoom; leave fullscreen. |
| `Home` | Reset to the video’s original 1:1 size, centred on screen. |
| `Up/Down/Left/Right` | Move the window by one grid step. |
| `Ctrl + Arrow` | Grow the window on that edge by one grid step. |
| `Alt + Arrow` | Shrink the window on that edge by one grid step. |
| `Shift + Arrow` | Move the zoomed region inside the window (a tenth of the view per step). |
| `PgUp / PgDown` | Shrink / enlarge the window by 10% (keeps the top-left corner). |
| `I / + / =` | Zoom in (Ctrl: enlarge the window around its centre). |
| `O / - / _` | Zoom out (Ctrl: shrink the window around its centre). |
| `Ctrl + Mouse Drag` | Move the zoomed region inside the window. |
| `Ctrl-C` | Exit the program (both terminal and GUI). |
<!-- /generated:keys -->

Window moves and resizes happen in grid steps (`-g`, default 20 px) and are clamped to the virtual desktop, so a window can sit on a second monitor left of or above the primary one. A held key repeats only moves, resizes and zoom; toggles (R, T, A, F, H, S, P …) act once per press.

## Mouse

<!-- generated:mouse -->
| Mouse | Action |
|---|---|
| `Wheel` | Zoom in (push the wheel away) or out (pull it toward you) around the cursor; a trackpad pinch on macOS too. |
| `Double Click` | Toggle fullscreen mode. |
| `Right Click` | Fit the window to the video (or the zoomed region). |
| `Middle Click` | Make what the window shows the zoomed region (unzoomed: cut the video to the window shape). |
| `Drag` | Move the window. |
| `Border Drag` | Resize the window. |
| `Top-Left Corner` | Show the control strip (stream, record, audio, help). |
<!-- /generated:mouse -->

A double-click on a control-strip icon counts as one click (it no longer also toggles fullscreen). Where the platform cannot move or resize a window itself (border resize on macOS, X11 without a window manager), Nova follows the mouse.

## Zoom

Pushing the wheel away zooms in and pulling it toward you zooms out, 10 % per notch, around the cursor (high-resolution wheels and touchpads by the fraction of a notch they report); **I**/**O** (or **+**/**−**) zoom around the centre. On macOS, "natural scrolling" is taken into account so the hand's movement means the same, and a trackpad pinch zooms by exactly what the fingers do. `NOVA_WHEEL_INVERT = 1` in `[env]` reverses the wheel. The zoomed region is a crop of the decoded frame, rendered at full window resolution, and it is remembered per camera (see [settings](#where-settings-are-stored)). **Esc** resets it.

The zoom is a *region* of the picture, not a magnification. Resizing the window keeps the region and shows it at the new size; where the window is wider or taller than the region, the picture around the region fills the extra space instead of black bars (the region itself is always shown whole; black appears only where the frame runs out). Narrowing the window back shows exactly the region again, so a window can be moved, enlarged to full screen and shrunk again without touching the zoom. **Ctrl-drag** and **Shift+Arrow** move the region inside the window; the wheel, **I**/**O** and these act on what is shown, so after a resize the surroundings that came into view become part of the region. Middle-click makes what is shown the region (unzoomed: cuts the video to the window's shape); right-click resizes the window to the region's own aspect ratio.

Recording and streaming always use the full frame, never the zoomed view.

## Audio

**A** (or `-a` at start) plays the camera's audio through PortAudio. Nova opens the device at the camera's sample rate and falls back to 48 kHz with resampling when the device refuses. On Linux, PipeWire/Pulse devices are preferred; `NOVA_AUDIO_DEVICE_INDEX` forces a device (`-l TRACE` lists them at start). Playback is best-effort live audio: it is not lip-synced to the display, but recordings and streams are.

## Snapshots

**S** saves the current frame as `snapshots/<camera>_YYYYMMDD_HHMMSS.png` (`--snapshot-dir` / `--output-dir` change the folder). The PNG is encoded off the GUI thread; the full frame is saved regardless of zoom.

## Where settings are stored

Per camera (keyed by a hash of the `-i` URL): window geometry and the zoom region. They are written within 2 s of every move, resize or zoom change, and once more when the window closes, on Ctrl-C and on `nova stop`. A fullscreen or maximized window keeps its normal place on record, and a camera reconnect never changes the zoom. When a monitor is unplugged or drops out while asleep, the windows keep their places on record and go back once the monitor returns (unless you moved them meanwhile); a remembered place on a monitor that is not connected opens on the primary screen. A layout (`nova layout save`) is a named snapshot of these values in `layouts/NAME.json`.

| OS | Location |
|---|---|
| Linux | `~/.config/judvi/Nova.conf` |
| Windows | registry `HKEY_CURRENT_USER\Software\judvi\Nova` |
| macOS | `~/Library/Preferences/com.judvi.Nova.plist` |

Resolved shortcut URLs (`onvif://`, `hik://`, `dahua://`, `xmeye://`, `reolink://`, `axis://`: [Camera brands](#camera-brands)) are cached for one day in `.uri_cache.json` next to `nova.py`, without the password. Only URLs built from the device's own answer are cached (`tapo://` and `unv://` need no cache), and a camera that gives no picture three times in a row is asked again. A password may contain `@` (`user:pa@ss@ip` works) or be percent-encoded.

Everything Nova and the launcher write (recordings, snapshots, logs, layouts, the pid file, the settings file) is created readable by you only (umask 077 on Linux and macOS): footage and logs are private on a shared machine. Files from older versions keep their permissions; `chmod -R go-rwx videos snapshots logs layouts` tightens them. If another account or a service (Samba, a media server, backups) reads `videos/`, set `NOVA_UMASK = 022` in `[env]` (and in the shell that runs `./nova`). Keep `cameras.ini` at `chmod 600`; the launcher warns when others can read it.

## Environment variables

| Variable | Meaning |
|---|---|
| `NOVA_TIMEOUT` | socket / read timeout in microseconds (default 5 s for camera input and 10 s for a stream target; `cameras.example.ini` sets 10 s). Also bounds how long shutdown can take while a camera answers but stalls in the RTSP handshake; a camera that is down is detected by a TCP probe first and stops at once. For a network stream target (`-t rtmp://…`, SRT, HLS) it is the write deadline: a connection that accepts nothing for that long ends the stream (connecting may take up to 30 s). |
| `NOVA_GPU_DEVICES` | GPU index, or a comma list (`0,1`). With a list, `nova start` / `nova run` deal the cameras over the GPUs in file order, so a camera always lands on the same one (14 cameras on `0,1` → 7 + 7); `--gpu N` in a camera's `opts` pins it. A bare `python nova.py` given a list picks one at random |
| `NOVA_AUDIO_DEVICE_INDEX` | force a PortAudio output device |
| `NOVA_WHEEL_INVERT` | `1` reverses the wheel's zoom direction (default: pushing the wheel away zooms in) |
| `NOVA_MIN_RECORD_INTERVAL` | the shortest `--record-interval` in seconds (default 60; the test suite lowers it) |
| `NOVA_UMASK` | permissions for new files, octal (default `077`: this user only; `022` lets other accounts read recordings and logs) |
| `NOVA_AV_LOG` | FFmpeg's own log threshold, raw: `panic fatal error warning info verbose debug trace quiet` (default: matched to `-l`, with known noise demoted; see Logs) |
| `QT_QPA_PLATFORM` | `offscreen` is set automatically for `--headless`; on a Wayland session Nova sets `xcb` (XWayland) itself, `wayland` asks for a native window |

## Logs

`-l` sets the level: `CRITICAL ERROR WARNING INFO VERBOSE DEBUG TRACE` (or a number). `INFO` shows connections, decoder/encoder choices and saved files; `DEBUG` adds the FFmpeg negotiation; `TRACE` adds ONVIF SOAP bodies, XMeye DVRIP packets and audio device lists.

FFmpeg's own messages come through at the matching level: its FATAL at `CRITICAL`, ERROR at `ERROR`, WARNING at `WARNING`, INFO at `INFO` and `VERBOSE`, VERBOSE at `DEBUG`, DEBUG at `TRACE`. A few that FFmpeg writes for every live camera are demoted to `DEBUG` (the errors of joining a stream between two keyframes, `non-existing PPS` / `no frame!` and their H.265 equivalents; the private NAL units some cameras send; the AAC encoder's statistics and FLV's header warnings on close) and the one-line-per-NAL-unit output is dropped; the list, with the source of each, is `AV_NOISE` in `logsync.py`. `NOVA_AV_LOG=verbose` (or `panic … trace`, `quiet`) sets FFmpeg's threshold directly and switches those rules off, for a bug report. Passwords and stream keys are masked in every log line, traceback and launcher output (`user:***@`, `password=***`, the last part of an `rtmp://` URL, SRT passphrase and streamid). `nova start` and `nova record` write each camera to `logs/ID.log`.
