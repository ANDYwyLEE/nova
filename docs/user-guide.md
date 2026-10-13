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
| `url` | anything FFmpeg can open: `rtsp://`, `rtmp://`, `srt://`, `http(s)://`, `udp://`, a file, or the shortcuts `onvif://user:pw@ip[:port][/0\|1]`, `xmeye://`, `hik://`, `dahua://`, `0` / `v4l2:///dev/video2` / `dshow://video=NAME` / `avfoundation://0` for capture devices |
| `name` | window title and the folder name under `videos/` and `snapshots/` (default: the section name) |
| `opts` | any `nova.py` options; appended to `[defaults] opts` |
| `enabled` | `no` keeps the camera out of `start` (it can still be started by id) |

Comments are whole lines starting with `;` or `#`. A `;` after a value is part of the value.

## The launcher: nova

`./nova` on Linux and macOS, `nova` on Windows (`nova.bat`). Both use the project `venv` automatically, so nothing needs activating; `cams.py` is the implementation.

```
nova start  [ID ...] [options]       a window per camera: every enabled camera, or the given ids
nova record [ID ...] [options]       the same without windows, recording from the first frame
nova run    [ID]     [options]       one camera in this terminal: log on screen, Ctrl-C quits
nova stop   [ID ...] [--timeout S]   stop cameras started by start/record (saves state like Ctrl-C)
nova restart [ID ...] [options]      stop, then start again as launched (record stays record, layout stays)
nova status                          launched cameras: running or exited, uptime, mode, gpu, layout
nova list                            cameras, enabled state and options (passwords masked)
nova -h                              help; `python nova.py --help` lists the camera options
```

- Words that do not start with `-` are camera ids; everything from the first option on is passed to `nova.py`: `nova start frontdoor -r` shows the window and records, `nova record yard -t rtmp://…` records without a window and streams.
- Options a camera should always get belong in its `cameras.ini` section (`opts = -r`), so a plain `nova start` does the right thing per camera.
- `start` and `record` detach from the terminal, write each camera's output to `logs/ID.log` and remember the process ids in `.nova-pids.json`. A camera that exits within the first seconds is reported with the end of its log instead of being called "running".
- A camera without a window that neither records nor streams can neither be seen nor controlled, so `start --headless` without `-r` or `-t` is refused; `record` is the shorthand for `--headless -r`.
- `stop` only touches processes it started and waits for them to exit cleanly (window geometry, zoom and open recordings are saved).
- `restart` stops the launched cameras (or the given ids) and starts them again the way they were launched: a camera from `record` stays headless, a window keeps its layout (`--layout NAME` switches), and the options from `cameras.ini` apply as always. Options typed after `start` are not remembered, so give them again. A camera that is not running is simply started.
- `status` lists every camera the launcher started: pid, running or exited, uptime, mode (window / headless, rec, stream), GPU and layout.
- `run` keeps one camera in the foreground: the log appears in the terminal (`nova run ID -l DEBUG` for diagnosis) and Ctrl-C quits. Without an id it runs `[defaults] camera`, or the first enabled camera.

## Layouts

Every window remembers its own place. A layout remembers the whole wall: which cameras are open, where each window sits, how big it is and which part of the picture it shows. Keep as many as you like (`work`, `away`, `one-monitor`, …).

```
nova layout save NAME        write the open cameras' windows to layouts/NAME.json
nova layout list             saved layouts and their cameras
nova start --layout NAME     open exactly the cameras of that layout, where they were saved
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

  The rules match the window title `Nova - NAME` (the camera's name from `cameras.ini` / `-n`), XWayland and native windows alike, so they keep working across restarts; the positions are the absolute coordinates Nova saved. Under a rule the window still remembers where you drag it, as usual. **NVIDIA**: with the proprietary driver (595 at the time of writing) a native Wayland Nova window stays invisible, because NVIDIA's EGL does not display frameless OpenGL windows on Wayland (decorated windows and Mesa drivers are fine); XWayland, the default, is not affected.

## Command-line options

`python nova.py -i URL [options]`. Generated from the in-app help:

<!-- generated:args -->
| Option | Meaning |
|---|---|
| `-i, --input` | Video source: rtsp/rtmp/srt URL, onvif://, xmeye://, hik://, dahua://, '0' (USB cam), dshow://video=NAME |
| `-n, --name, --camera-name` | Logical camera name for output recording folders. |
| `--opengl` | Enable hardware acceleration using OpenGL (recommended for high performance). |
| `--accel` | HW Acceleration backend. cuda, qsv, vaapi, videotoolbox, d3d11va. |
| `--gpu` | GPU index for HW decode/encode (0, 1, or a list 0,1 to pick at random; default: NOVA_GPU_DEVICES). |
| `-f, --fps` | Target video frame rate (default: 30). |
| `-b, --border` | Enable the colored window border. (default: no border line) |
| `-g, --grid-size` | Grid size for window alignment in pixels (default: 20). |
| `--geometry` | Initial window placement x,y,w,h; overrides the remembered one (nova start --layout uses it). |
| `--zoom` | Initial zoom region as ratios rx,ry,rw,rh of the picture (0,0,1,1 = whole picture). |
| `-a, --audio, --enable-audio` | Enable audio playback automatically on startup. |
| `--headless, --no-gui` | Run in headless mode without showing the visual camera window. |
| `-r, --record, --auto-record` | Start recording automatically on startup. |
| `-e, --encoder` | Explicitly specify video encoder (e.g., av1_nvenc, hevc_nvenc) |
| `--record-interval` | Recording segment length (e.g. 10m, 1h, 3600s; default: 30m). |
| `--output-dir` | Base directory for snapshots and recordings. |
| `--snapshot-dir` | Directory for snapshots (overrides --output-dir for snapshots). |
| `--recording-dir` | Directory for recorded videos (overrides --output-dir for recordings). |
| `--ffmpeg` | FFmpeg options: -rtsp_transport udp -b:v 4M -crf 23 -g 60 -s WxH -r N -vf transpose=1 -pix_fmt -ar -ac -b:a -metadata k=v -movflags (README lists what works) |
| `-t, --target` | Live stream URL (e.g. YouTube, Twitch) or output path |
| `-l, --log-level` | Logging level (NAME or NUMBER; e.g., DEBUG, INFO, 5, 10; default: INFO). |
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
| LIVE | streaming to the `-t` target (shown only when a target is configured) |
| camera | recording |
| speaker | audio playback |
| ? | help overlay (same as **H**) |

Status text in the window ("Connecting…", "Failed: …", "Stream error: …") comes from the decoder thread; Nova reconnects on its own every few seconds.

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
| `P` | Print the stream URL to the terminal. |
| `ESC` | Reset the zoom; leave fullscreen. |
| `Home` | Reset to the video’s original 1:1 size, centred on screen. |
| `Up/Down/Left/Right` | Move the window by one grid step. |
| `Ctrl + Arrow` | Grow the window on that edge by one grid step. |
| `Alt + Arrow` | Shrink the window on that edge by one grid step. |
| `PgUp / PgDown` | Shrink / enlarge the window by 10% (keeps the top-left corner). |
| `I / + / =` | Zoom in (Ctrl: enlarge the window around its centre). |
| `O / - / _` | Zoom out (Ctrl: shrink the window around its centre). |
| `Ctrl-C` | Exit the program (both terminal and GUI). |
<!-- /generated:keys -->

Window moves and resizes happen in grid steps (`-g`, default 20 px) and are clamped to the virtual desktop, so a window can sit on a second monitor left of or above the primary one.

## Mouse

<!-- generated:mouse -->
| Mouse | Action |
|---|---|
| `Wheel` | Zoom in or out around the cursor. |
| `Double Click` | Toggle fullscreen mode. |
| `Right Click` | Fit the window to the video (or the zoomed region). |
| `Middle Click` | Fit the zoomed region to the window shape. |
| `Drag` | Move the window. |
| `Border Drag` | Resize the window. |
| `Top-Left Corner` | Show the control strip (stream, record, audio, help). |
<!-- /generated:mouse -->

## Zoom

The wheel zooms around the cursor; **I**/**O** (or **+**/**−**) zoom around the centre. The zoomed region is a crop of the decoded frame, rendered at full window resolution, and it is remembered per camera (see [settings](#where-settings-are-stored)). **Esc** resets it. Middle-click fits the region to the window's aspect ratio; right-click resizes the window to the region's aspect ratio instead.

Recording and streaming always use the full frame, never the zoomed view.

## Audio

**A** (or `-a` at start) plays the camera's audio through PortAudio. Nova opens the device at the camera's sample rate and falls back to 48 kHz with resampling when the device refuses. On Linux, PipeWire/Pulse devices are preferred; `NOVA_AUDIO_DEVICE_INDEX` forces a device (`-l TRACE` lists them at start). Playback is best-effort live audio: it is not lip-synced to the display, but recordings and streams are.

## Snapshots

**S** saves the current frame as `snapshots/<camera>_YYYYMMDD_HHMMSS.png` (`--snapshot-dir` / `--output-dir` change the folder). The PNG is encoded off the GUI thread; the full frame is saved regardless of zoom.

## Where settings are stored

Per camera (keyed by a hash of the `-i` URL): window geometry and the zoom region. They are written within 2 s of every move, resize or zoom change, and once more when the window closes, on Ctrl-C and on `nova stop`. A layout (`nova layout save`) is a named snapshot of these values in `layouts/NAME.json`.

| OS | Location |
|---|---|
| Linux | `~/.config/judvi/Nova.conf` |
| Windows | registry `HKEY_CURRENT_USER\Software\judvi\Nova` |
| macOS | `~/Library/Preferences/com.judvi.Nova.plist` |

Resolved `onvif://` / `xmeye://` / `hik://` / `dahua://` URLs are cached for one day in `.uri_cache.json` next to `nova.py`, without the password.

## Environment variables

| Variable | Meaning |
|---|---|
| `NOVA_TIMEOUT` | socket / read timeout in microseconds (default 5 s; `cameras.ini` sets 10 s). Also bounds how long shutdown can take while a camera answers but stalls in the RTSP handshake; a camera that is down is detected by a TCP probe first and stops at once. |
| `NOVA_GPU_DEVICES` | GPU index, or a comma list (`0,1`). With a list, `nova start` / `nova run` deal the cameras over the GPUs in file order, so a camera always lands on the same one (14 cameras on `0,1` → 7 + 7); `--gpu N` in a camera's `opts` pins it. A bare `python nova.py` given a list picks one at random |
| `NOVA_AUDIO_DEVICE_INDEX` | force a PortAudio output device |
| `QT_QPA_PLATFORM` | `offscreen` is set automatically for `--headless`; on a Wayland session Nova sets `xcb` (XWayland) itself, `wayland` asks for a native window |

## Logs

`-l` sets the level: `CRITICAL ERROR WARNING INFO VERBOSE DEBUG TRACE` (or a number). `INFO` shows connections, decoder/encoder choices and saved files; `DEBUG` adds the FFmpeg negotiation; `TRACE` adds ONVIF SOAP bodies and audio device lists. Passwords are masked everywhere. `nova start` and `nova record` write each camera to `logs/ID.log`.
