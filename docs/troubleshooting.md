# Troubleshooting and performance

- [Reading the log](#reading-the-log)
- [Connection and decoding](#connection-and-decoding)
- [Display](#display)
- [Recording and streaming](#recording-and-streaming)
- [Audio](#audio)
- [Performance tips](#performance-tips)
- [Known limitations](#known-limitations)

## Reading the log

Run one camera in the foreground with `-l DEBUG` (`./nova run ID -l DEBUG`) and read the lines in this order: `[VideoWorker] av.open(…)` (connection), `HW decoder:` / `software decoder` (decoder choice), `First frame (…)` (pixel format and size), then `[AVWriter]` lines when recording or streaming starts. Passwords are masked in all log output, so logs can be shared.

## Connection and decoding

| Message | Meaning | What to do |
|---|---|---|
| `Unreachable: host:port …` or `Failed: Connection timed out` (overlay) | camera down or unreachable | check IP/port, firewall; Nova retries every 10 s; `NOVA_TIMEOUT` sets the socket timeout |
| `Failed: 401 Unauthorized` / `Invalid data found` | wrong credentials or wrong path | try the camera's ONVIF address: `onvif://user:pw@ip` resolves the correct RTSP path |
| `--accel=X is not supported by this FFmpeg/PyAV build` | the wheel lacks that hwaccel (e.g. `vaapi`) | use `cuda`/`qsv`/`d3d11va`, or build FFmpeg ([custom-ffmpeg.md](custom-ffmpeg.md)) |
| `No working HW decoder for --accel=cuda (reason). Falling back to software (codec); hardware is tried again in N min` | the hardware decoder cannot be used: wrong `--gpu`, GPU out of memory, or a stream it cannot decode (rawvideo webcams, MJPEG 4:2:2, an unusual profile) | the picture continues in software without reconnecting; hardware is tried again after 5, 10, 20, 40, then every 60 min. Check `nvidia-smi` and `--gpu` / `NOVA_GPU_DEVICES` if it keeps happening |
| `Reconnecting: no video for N s while audio keeps flowing` / `no picture for N s` / `N video packets in a row failed to decode` | the camera's video stalled or is broken while the connection stays up (single damaged packets are skipped) | Nova reconnects by itself; repeated: lower the camera's bitrate or check its network |
| `cannot read host:port from the URL … must be percent-encoded (%2F %3F %23 %40 %5B %5D)` | the password contains `/ ? # @ [ ]` unencoded | write those characters percent-encoded in `cameras.ini` (`pa/ss` → `pa%2Fss`) |
| `non-existing PPS 0 referenced` / `no frame!` (once) | joined the stream mid-GOP | normal; decoding starts at the next keyframe |
| `Stream error:` in the window, `[VideoWorker] Stream exception:` in the log, every few seconds | the camera drops the connection | try `--ffmpeg "-rtsp_transport udp"` (or `tcp` if you set udp); lower the camera's bitrate |
| `Failed: URI resolve` in the window, `[ONVIF] ❌ All attempts failed for IP` in the log, with `onvif://` | ONVIF port not found or credentials rejected | give the port explicitly (`onvif://user:pw@ip:8080`), check with `-l TRACE` which SOAP calls fail |
| decoder `ignored unknown --ffmpeg options: {…}` | option not valid for that decoder | `-resize` is cuvid-only, `-skip_frame` is software-only; see [ffmpeg-options.md](ffmpeg-options.md) |

## Display

| Symptom | Cause | Fix |
|---|---|---|
| black window, log says `Shader Error` or `initializeGL Error` | no OpenGL 3.3 core context (remote desktop, old GPU, some VMs) | drop `--opengl` (QPainter renderer) or install a Mesa with llvmpipe |
| windows open centred or tiled, keyboard moves do nothing | native Wayland window (`QT_QPA_PLATFORM=wayland`): the compositor ignores client-side positioning | drop that setting (Nova then uses XWayland, where GNOME and KDE honour positions), or on sway / Hyprland / KDE import rules from `nova layout export NAME …` (user guide, Layouts) |
| window exists but shows nothing (native Wayland, NVIDIA, `--opengl`) | an older Nova: the OpenGL draw buffer was left at `GL_NONE` (fixed 2026-10-13) | update, or run under XWayland: unset `QT_QPA_PLATFORM` (the default) |
| colours slightly off (greenish skin tones) | camera signals no colour space | Nova assumes BT.709 above 576 lines, BT.601 below; cameras that lie cannot be helped without `-vf colorspace=…` |
| high CPU with `--opengl` and `--accel cuda` | the stream is not H.264/HEVC (MJPEG cameras decode in software) | `-f 10`; or `--ffmpeg "-skip_frame nokey"` for a cheap preview of a camera you do not record (recordings and streams get the same one frame per GOP) |
| the window is huge on a 4K camera | windows default to the saved geometry, first run uses 320×180 | **Home** gives 1:1, right-click fits, **PgUp** shrinks |
| control strip never appears | the mouse must enter the top-left corner of the *video area* (not the letterbox) | press **H** instead, or widen the window |

## Recording and streaming

| Message | Meaning | What to do |
|---|---|---|
| `[AVRecorder] recording failed: …` then `[AVRecorder] restarting in N s` | the encoder or the disk failed | the message says which (`No space left`, `Permission denied`, an encoder error); recording starts again by itself (5, 10, 20, 40 s, then every 60 s) once the cause is gone |
| `[AVRecorder] not recording: … another Nova process (pid N) …` | a second Nova runs the same camera and its recorder holds the folder | stop one of them (`nova status`, `ps`); this one takes over within a minute after the other stops |
| `segment left by a crash kept: …_recovered.mp4` | the previous run was killed (power loss, OOM, `kill -9`) mid-segment | nothing: the file plays up to its last fragment |
| `cannot rename … it keeps its name, the next segment is written next to it` | the finished segment could not be renamed (permissions, a file locked by a player on Windows) | the rename is tried again later; the file is complete under its temporary name |
| `⚠️ h264_nvenc does not open at WxH (…); using libx264` | the probe passed but the real size did not open (NVENC session limit with many cameras, a GPU fault) | nothing to do; the encoder is tried again after an hour. Fewer NVENC sessions: record some cameras with `-e libx264` |
| `⚠️ -e hevc_nvenc: flv carries h264 only` | the forced encoder's codec does not fit the target (RTMP is H.264) | drop `-e` for that camera, or use HLS for HEVC |
| `audio encoder … does not open at N Hz …; no audio track` | the camera's audio format cannot be encoded as set (`-ar`/`-ac`) | change or drop `-ar`/`-ac`; video is unaffected |
| `EncoderManager ⚠️ h264_nvenc rejects your --ffmpeg options {…}` | option names differ per encoder | NVENC: `-preset p1…p7 -tune hq/ll/ull -cq N`; libx264: `-preset ultrafast…veryslow -crf N -tune zerolatency` |
| `video encoder … ignored unknown --ffmpeg options` | the key is not an option of that encoder | see the tables in [ffmpeg-options.md](ffmpeg-options.md) |
| `… are ffmpeg command-line options without a library equivalent; ignored` | `-t -ss -map -re -vsync …` only exist in the `ffmpeg` program | use `--record-interval`, `-r`, `-vf` instead |
| `-vf: filter 'drawtext' is not available in this FFmpeg build` | the PyPI wheel has no libfreetype | custom build, or burn text in later with ffmpeg |
| `empty segment discarded` | nothing was written during the segment (no frame arrived, or the file could not be started) | the camera was down, or see the error before it; nothing lost |
| recordings play too fast / audio drifts | fixed in the 2026-10-11 release (timestamps now follow the camera clock) | update |
| YouTube: "no video" | the encoder produced AV1/HEVC for RTMP (old release) or the key is wrong | update; RTMP now always gets H.264, also with `-e hevc_nvenc` set |
| `[AVStreamer] write timed out, giving up` then `reconnecting in N s` | the ingest server or the uplink stopped accepting data for `NOVA_TIMEOUT` | nothing: Nova reconnects by itself (5 s, 10 s, … then every 60 s) while the gray LIVE icon shows; press `T` to stop trying |
| `[AVStreamer] cannot open output: …` repeating | the connection is refused at every attempt: wrong key, server down, DNS, port 1935 blocked | fix the cause; the next attempt (at most a minute later) picks it up |
| `AVStreamer worker stopped: [Errno 5] Input/output error` right after start | the ingest server closed the connection: wrong key or unsupported codec | check the key; for RTMP keep H.264 |

## Audio

| Message | Meaning | What to do |
|---|---|---|
| `[AudioWorker] cannot open an audio output: …` (once; retried every 3 s) / `Audio stream open error` | no usable PortAudio output device, or the sample rate is refused | `NOVA_AUDIO_DEVICE_INDEX=N` (devices are listed with `-l TRACE`); Nova already retries at 48 kHz |
| crackling, drop-outs | the audio thread is starved or the device buffer is small | close other audio apps; on Linux prefer the PipeWire/Pulse device (chosen automatically when present) |
| no sound but the icon is on | the camera stream has no audio track | the window title/log shows `audio=none`; check the camera's audio setting |

## Performance tips

- **One GPU, many cameras**: NVDEC on a single RTX 4090 decodes well over 20 1080p streams; the limit is usually the display rate. `-f 15` halves the upload to the GPU without touching recordings.
- **Two GPUs**: `NOVA_GPU_DEVICES = 0,1` in `[env]`; `nova start` deals the cameras over them in turn and `nova list` shows who gets which. `opts = --gpu 1` pins a camera. `nvidia-smi` lists one process per camera under the GPU it uses.
- **Big cameras, small windows**: `--ffmpeg "-resize 960x540"` (cuvid) scales inside the decoder, so display, recording and streaming all work on the smaller frame.
- **Software decoding**: `--ffmpeg "-threads 2"` per camera avoids 15 processes each spawning 32 threads; `-skip_loop_filter all` trades a little quality for ~20 % CPU; `-skip_frame nokey` shows one frame per GOP almost for free (and records and streams only those frames, so not for a camera you record).
- **Recording cost**: NVENC is nearly free; libx264 `-preset veryfast` or `ultrafast` when there is no hardware encoder.
- **A camera that is down**: the window shows `Unreachable: host:port …` and Nova retries every 10 s. The host is probed with a plain TCP connect before the RTSP open, so `nova stop` ends such a camera at once. Only a camera that *answers* but then stalls in the RTSP handshake can hold a stop for up to `NOVA_TIMEOUT` (that open cannot be interrupted earlier).
- **Start-up storms**: `nova start` staggers starts (`stagger = 0.5`) and encoders are probed under a cross-process lock, so 15 cameras can start at once without fighting over the GPU.

## Known limitations

- No PTZ control, motion detection or event recording; Nova is a viewer/recorder.
- One window per process by design; there is no mosaic window (arrange windows on the grid instead).
- Display audio is live, not lip-synced to the picture; recordings and streams are in sync.
- Frame-rate-changing filters (`fps`, `minterpolate`) are not supported in `-vf`; use `-r`.
- SRT and VAAPI need a custom FFmpeg build; `drawtext` needs libfreetype (not in the wheel).
- Wayland: Nova runs under XWayland by default; GNOME and KDE then honour its positions, sway and Hyprland need rules from `nova layout export`.
- Windows capture devices need a device name (`dshow://video=NAME`); numeric ids work on Linux (`0` = `/dev/video0`) and macOS.
