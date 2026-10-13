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
- `--headless -r` records without a window (servers, cron jobs); `nova record [ID ...]` is the shorthand for every enabled camera in `cameras.ini`.
- Stopping Nova (window close, Ctrl-C, `nova stop`) closes the current segment cleanly.
- **A failure does not end recording.** When the disk is full, the folder becomes unwritable or the encoder fails, the segment is closed and recording starts again after 5, 10, 20, 40 s and then every 60 s for as long as it is switched on; meanwhile the camera icon is gray and `nova status` shows `record waiting`. Free the space and the next attempt picks it up.
- One process records into a camera's folder at a time (a lock file, `.nova-recording.lock`): a second Nova for the same camera (e.g. `nova run X` while `nova start` runs X) logs `not recording: … another Nova process (pid N)` and waits instead of writing into the same file.

## Files and segments

```
videos/<camera name>/YYYYMMDD_HHMM_HHMM.mp4      start and end time, local clock
videos/<camera name>/<camera name>_output.mp4    the segment being written right now
videos/<camera name>/YYYYMMDD_HHMM_recovered.mp4 a segment left by a crash (power loss, kill -9), kept
videos/<camera name>/<camera name>_output_1.mp4  only while an earlier segment could not be renamed
videos/<camera name>/.nova-recording.lock        held by the process that records this camera
```

- `--record-interval` (default `30m`; `10m`, `1h`, `3600s`) sets the segment length. Segments are aligned to the local clock: with 30 minutes, files start at :00 and :30, so the first segment after starting is shorter; intervals of a day or more start at local midnight, and daylight-saving changes are followed (on the night clocks go back, `1h` segments start at 02:00 twice). A segment cut by an outage is named by its last frame.
- The folder is `--recording-dir`, else `--output-dir`, else `./videos`; the camera name comes from `-n` / `name` in `cameras.ini`, else the host (and port) of the URL.
- A segment nothing was written to (camera never connected, or the file could not be started) is deleted instead of being renamed; a segment that only got audio is kept. A name collision (two segments ending in the same minute) gets a `_1` suffix rather than overwriting.

## Why fragmented MP4

Segments are written with `movflags +frag_keyframe+empty_moov`: the index is written per fragment (every keyframe, 2 s by default) instead of once at the end. If the process is killed or the machine loses power, everything up to the last complete fragment plays, and the next start keeps that file as `YYYYMMDD_HHMM_recovered.mp4` (named after its last write) instead of overwriting it; only the current fragment and up to ~32 KB of buffered data are lost. VLC, ffmpeg, browsers and current players handle fragmented MP4; a very old player that needs a classic index can be served with

```bash
ffmpeg -i 20261011_1500_1530.mp4 -c copy -movflags +faststart classic.mp4
```

or record classic files from the start with `--ffmpeg "-movflags +faststart"` (then a crash loses the whole segment).

## Timestamps and A/V sync

Nova stamps every frame from the camera's own clock (RTP timestamps), not by counting frames. The consequences:

- A camera that announces 25 fps but delivers 12 produces a file that plays at the right speed, with audio in sync.
- Dropped or missing frames become short freezes, not a sped-up timeline.
- A reconnect (network outage, camera reboot) re-anchors the stream to wall time: the gap is preserved in the file and audio/video stay aligned.
- Audio and video from the same camera share one clock inside each segment; the camera's audio is resampled when the encoder needs a different rate, and when its format changes after a reconnect.
- Whether a file gets an audio track is decided by the camera's first audio frame: the writer holds up to 2 s of video for it (not at all for a camera without an audio stream).

## Which encoder is used

Encoders are probed once per process, in this order, and the first that opens is used:

| output | preferred families |
|---|---|
| `.mp4` / `.mov` / `.mkv` | the camera's own codec family first (H.264 camera → H.264 file), then H.264, HEVC, AV1 |
| RTMP (`flv`) | H.264 only |
| HLS / MPEG-TS | H.264, HEVC |

Order: `-e` / `-c:v` (only if the output can carry that codec: `-e hevc_nvenc` is ignored with a warning for RTMP), then with `--accel` the matching hardware encoders (`cuda` → NVENC, `qsv` → Quick Sync, `videotoolbox`, AMF) for each allowed family, then `libx264` and the other software encoders (`libx265`, `libsvtav1`). Without `--accel` only software encoders are used. An encoder that passed the probe but does not open at the real size (NVENC's session limit, a GPU fault) is skipped for an hour and the writer continues with the next one, so no frame is lost (`X does not open at WxH; using Y`); an audio encoder that cannot open leaves the audio track out. A hardware encoder that rejects your `--ffmpeg` options is skipped with a warning (`h264_nvenc rejects your --ffmpeg options …`).

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
# HEVC with NVENC
python nova.py -i rtsp://... -r -e hevc_nvenc
```

## Disk usage and housekeeping

Size ≈ bitrate × time: 4 Mbit/s is about 1.8 GB per hour and 43 GB per day per camera; `-b:v`/`-cq` and `-r` are the levers. Nova does not delete old recordings. A daily cron job keeps a rolling window:

```bash
# keep 14 days
0 3 * * * find /path/to/nova/videos -name '*.mp4' -mtime +14 -delete
```

On Windows, a scheduled task running `forfiles /P videos /S /M *.mp4 /D -14 /C "cmd /c del @path"` does the same.
