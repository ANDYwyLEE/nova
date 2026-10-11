# Recording

- [Starting and stopping](#starting-and-stopping)
- [Files and segments](#files-and-segments)
- [Why fragmented MP4](#why-fragmented-mp4)
- [Timestamps and A/V sync](#timestamps-and-av-sync)
- [Which encoder is used](#which-encoder-is-used)
- [Tuning](#tuning)
- [Disk usage and housekeeping](#disk-usage-and-housekeeping)

## Starting and stopping

- **R** (or F7, or the camera icon in the control strip) toggles recording.
- `-r` starts recording as soon as the first frame arrives; put it in a camera's `opts` in `cameras.ini` to record that camera permanently.
- `--headless -r` records without a window (servers, cron jobs).
- Stopping Nova (window close, Ctrl-C, `cams.py stop`) closes the current segment cleanly.

## Files and segments

```
videos/<camera name>/YYYYMMDD_HHMM_HHMM.mp4      start and end time, local clock
videos/<camera name>/<camera name>_output.mp4    the segment being written right now
```

- `--record-interval` (default `30m`; `10m`, `1h`, `3600s`) sets the segment length. Segments are aligned to the wall clock: with 30 minutes, files start at :00 and :30, so the first segment after starting is shorter.
- The folder is `--recording-dir`, else `--output-dir`, else `./videos`; the camera name comes from `-n` / `name` in `cameras.ini`, else the host (and port) of the URL.
- A segment with no frames (camera never connected) is deleted instead of being renamed. A name collision (two segments ending in the same minute) gets a `_1` suffix rather than overwriting.

## Why fragmented MP4

Segments are written with `movflags +frag_keyframe+empty_moov`: the index is written per fragment (every keyframe, 2 s by default) instead of once at the end. If the process is killed or the machine loses power, everything up to the last complete fragment plays. VLC, ffmpeg, browsers and current players handle fragmented MP4; a very old player that needs a classic index can be served with

```bash
ffmpeg -i 20261011_1500_1530.mp4 -c copy -movflags +faststart classic.mp4
```

or record classic files from the start with `--ffmpeg "-movflags +faststart"` (then a crash loses the whole segment).

## Timestamps and A/V sync

Nova stamps every frame from the camera's own clock (RTP timestamps), not by counting frames. The consequences:

- A camera that announces 25 fps but delivers 12 produces a file that plays at the right speed, with audio in sync.
- Dropped or missing frames become short freezes, not a sped-up timeline.
- A reconnect (network outage, camera reboot) re-anchors the stream to wall time: the gap is preserved in the file and audio/video stay aligned.
- Audio and video from the same camera share one clock inside each segment; the camera's audio is resampled when the encoder needs a different rate.

## Which encoder is used

Encoders are probed once per process, in this order, and the first that opens is used:

| output | preferred families |
|---|---|
| `.mp4` / `.mov` / `.mkv` | the camera's own codec family first (H.264 camera → H.264 file), then H.264, HEVC, AV1 |
| RTMP (`flv`) | H.264 only |
| HLS / MPEG-TS | H.264, HEVC |

Within a family: the hardware encoder matching `--accel` (`cuda` → NVENC, `qsv` → Quick Sync, `videotoolbox`, AMF), then the other hardware encoders of the platform, then `libx264` (and `libx265`, `libsvtav1`). `-e hevc_nvenc` forces a specific encoder; `-c:v` inside `--ffmpeg` does the same. A hardware encoder that rejects your `--ffmpeg` options is skipped with a warning (`h264_nvenc rejects your --ffmpeg options …`).

Defaults applied unless you override them: keyframe interval 2 s (`-g`), `hvc1` tag for HEVC in MP4, pixel format `nv12` for hardware encoders and `yuv420p` for software ones.

## Tuning

All of these go into `--ffmpeg "…"` (details and the full table: [ffmpeg-options.md](ffmpeg-options.md)):

```bash
-b:v 4M -maxrate 4M -bufsize 8M     # capped bitrate (both encoders)
-crf 23 -preset veryfast            # libx264 quality mode
-rc vbr -cq 28 -preset p5           # NVENC quality mode
-g 50 -bf 0                         # keyframe every 50 frames, no B-frames
-r 10                               # keep 10 frames per second (drops frames, audio untouched)
-s 1280x720                         # downscale before encoding
-vf transpose=1                     # rotate a camera mounted sideways
-pix_fmt yuv444p                    # 4:4:4 if the encoder supports it
-ac 1 -ar 16000 -b:a 32k            # small mono audio track
-an                                 # no audio track
-metadata title=Lounge              # container metadata
```

Examples:

```bash
# everyday archive at 10 fps, 1 Mbit/s, 1-hour files
python nova.py -i rtsp://... -r --record-interval 1h --ffmpeg "-r 10 -b:v 1M"
# HEVC with NVENC, keyframe-only decode on the display side (saves CPU on a busy box)
python nova.py -i rtsp://... -r -e hevc_nvenc --ffmpeg "-skip_frame nokey"
```

## Disk usage and housekeeping

Size ≈ bitrate × time: 4 Mbit/s is about 1.8 GB per hour and 43 GB per day per camera; `-b:v`/`-cq` and `-r` are the levers. Nova does not delete old recordings. A daily cron job keeps a rolling window:

```bash
# keep 14 days
0 3 * * * find /path/to/nova/videos -name '*.mp4' -mtime +14 -delete
```

On Windows, a scheduled task running `forfiles /P videos /S /M *.mp4 /D -14 /C "cmd /c del @path"` does the same.
