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
| `Connecting… / Failed: Connection timed out` (overlay) | camera unreachable | check IP/port, firewall; Nova retries every 10 s; `NOVA_TIMEOUT` sets the socket timeout |
| `Failed: 401 Unauthorized` / `Invalid data found` | wrong credentials or wrong path | try the camera's ONVIF address: `onvif://user:pw@ip` resolves the correct RTSP path |
| `--accel=X is not supported by this FFmpeg/PyAV build` | the wheel lacks that hwaccel (e.g. `vaapi`) | use `cuda`/`qsv`/`d3d11va`, or build FFmpeg ([custom-ffmpeg.md](custom-ffmpeg.md)) |
| `No working HW decoder for --accel=cuda. Falling back to software` | the GPU is not usable from this process | `nvidia-smi` works? driver loaded? `--gpu` / `NOVA_GPU_DEVICES` points at an existing GPU? |
| `produced no frames after N keyframes; reconnecting with software decoding` | the hardware decoder opened but cannot decode this stream (unusual profile/level, 10-bit, MJPEG) | software decoding is used from now on; nothing to do unless CPU is short |
| `non-existing PPS 0 referenced` / `no frame!` (once) | joined the stream mid-GOP | normal; decoding starts at the next keyframe |
| `Stream error: … ; reconnecting` every few seconds | the camera drops the connection | try `--ffmpeg "-rtsp_transport udp"` (or `tcp` if you set udp); lower the camera's bitrate |
| `URI resolve failed` with `onvif://` | ONVIF port not found or credentials rejected | give the port explicitly (`onvif://user:pw@ip:8080`), check with `-l TRACE` which SOAP calls fail |
| decoder `ignored unknown --ffmpeg options: {…}` | option not valid for that decoder | `-resize` is cuvid-only, `-skip_frame` is software-only; see [ffmpeg-options.md](ffmpeg-options.md) |

## Display

| Symptom | Cause | Fix |
|---|---|---|
| black window, log says `Shader Error` or `initializeGL Error` | no OpenGL 3.3 core context (remote desktop, old GPU, some VMs) | drop `--opengl` (QPainter renderer) or install a Mesa with llvmpipe |
| windows all open at the same place, keyboard moves do nothing | Wayland compositor ignores client-side positioning | run under XWayland: `QT_QPA_PLATFORM=xcb` (put it in `[env]`) |
| colours slightly off (greenish skin tones) | camera signals no colour space | Nova assumes BT.709 above 576 lines, BT.601 below; cameras that lie cannot be helped without `-vf colorspace=…` |
| high CPU with `--opengl` and `--accel cuda` | the stream is not H.264/HEVC (MJPEG cameras decode in software) | `--ffmpeg "-skip_frame nokey"` for a cheap preview, or `-f 10` |
| the window is huge on a 4K camera | windows default to the saved geometry, first run uses 320×180 | **Home** gives 1:1, right-click fits, **PgDown** shrinks |
| control strip never appears | the mouse must enter the top-left corner of the *video area* (not the letterbox) | press **H** instead, or widen the window |

## Recording and streaming

| Message | Meaning | What to do |
|---|---|---|
| `[AVRecorder] worker stopped: …` | the encoder or the disk failed | the message says which; `No space left`, `Permission denied`, or an encoder error |
| `EncoderManager ⚠️ h264_nvenc rejects your --ffmpeg options {…}` | option names differ per encoder | NVENC: `-preset p1…p7 -tune hq/ll/ull -cq N`; libx264: `-preset ultrafast…veryslow -crf N -tune zerolatency` |
| `video encoder … ignored unknown --ffmpeg options` | the key is not an option of that encoder | see the tables in [ffmpeg-options.md](ffmpeg-options.md) |
| `… are ffmpeg command-line options without a library equivalent; ignored` | `-t -ss -map -re -vsync …` only exist in the `ffmpeg` program | use `--record-interval`, `-r`, `-vf` instead |
| `-vf: filter 'drawtext' is not available in this FFmpeg build` | the PyPI wheel has no libfreetype | custom build, or burn text in later with ffmpeg |
| `empty segment discarded` | no frame arrived during the segment | the camera was down; nothing lost |
| recordings play too fast / audio drifts | fixed in the 2026-10-11 release (timestamps now follow the camera clock) | update |
| YouTube: "no video" | the encoder produced AV1/HEVC for RTMP (old release) or the key is wrong | update; RTMP now always gets H.264 |
| `AVStreamer worker stopped: [Errno 5] Input/output error` right after start | the ingest server closed the connection: wrong key or unsupported codec | check the key; for RTMP keep H.264 |

## Audio

| Message | Meaning | What to do |
|---|---|---|
| `Audio open failed` / `Audio stream open error` | no usable PortAudio output device, or the sample rate is refused | `NOVA_AUDIO_DEVICE_INDEX=N` (devices are listed with `-l TRACE`); Nova already retries at 48 kHz |
| crackling, drop-outs | the audio thread is starved or the device buffer is small | close other audio apps; on Linux prefer the PipeWire/Pulse device (chosen automatically when present) |
| no sound but the icon is on | the camera stream has no audio track | the window title/log shows `audio=none`; check the camera's audio setting |

## Performance tips

- **One GPU, many cameras**: NVDEC on a single RTX 4090 decodes well over 20 1080p streams; the limit is usually the display rate. `-f 15` halves the upload to the GPU without touching recordings.
- **Two GPUs**: `NOVA_GPU_DEVICES = 0,1` in `[env]`; `nova start` deals the cameras over them in turn and `nova list` shows who gets which. `opts = --gpu 1` pins a camera. `nvidia-smi` lists one process per camera under the GPU it uses.
- **Big cameras, small windows**: `--ffmpeg "-resize 960x540"` (cuvid) scales inside the decoder, so display, recording and streaming all work on the smaller frame.
- **Software decoding**: `--ffmpeg "-threads 2"` per camera avoids 15 processes each spawning 32 threads; `-skip_loop_filter all` trades a little quality for ~20 % CPU; `-skip_frame nokey` shows one frame per GOP almost for free.
- **Recording cost**: NVENC is nearly free; libx264 `-preset veryfast` or `ultrafast` when there is no hardware encoder.
- **Start-up storms**: `nova start` staggers starts (`stagger = 0.5`) and encoders are probed under a cross-process lock, so 15 cameras can start at once without fighting over the GPU.

## Known limitations

- No PTZ control, motion detection or event recording; Nova is a viewer/recorder.
- One window per process by design; there is no mosaic window (arrange windows on the grid instead).
- Display audio is live, not lip-synced to the picture; recordings and streams are in sync.
- Frame-rate-changing filters (`fps`, `minterpolate`) are not supported in `-vf`; use `-r`.
- SRT and VAAPI need a custom FFmpeg build; `drawtext` needs libfreetype (not in the wheel).
- Wayland needs XWayland (`QT_QPA_PLATFORM=xcb`) for window placement to work.
- Windows capture devices need a device name (`dshow://video=NAME`); numeric ids work on Linux (`0` = `/dev/video0`) and macOS.
