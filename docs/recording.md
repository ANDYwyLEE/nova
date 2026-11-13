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
- **Sound that stops is fetched again.** Some cameras stop sending the audio of a connection after minutes or hours while the video goes on, and send it again to a new connection. When audio that was flowing has been missing for 30 s, Nova reconnects (`no audio for 31 s while video keeps flowing; reconnecting` in the log; the picture pauses for a few seconds and the segment continues). A camera that drops its audio again soon after every connect is asked less and less often, up to once an hour; `NOVA_AUDIO_STALL` sets the wait in seconds, `0` leaves such recordings silent. A segment that got no sound at all is named in the log (`this segment has no sound`).
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
- A reconnect (network outage, camera reboot) re-anchors the stream to wall time (or to the point the file has reached, where that is later): the gap is preserved in the file and audio/video stay aligned. The new connection's audio arrives ahead of its first keyframe and goes into the segment whose time it falls in: past a boundary it waits for the video frame that opens the next segment.
- **Timestamps that step are not written as gaps.** Some cameras send RTCP sender reports that do not fit their own RTP timestamps (four cameras of one make here: a report per stream every 7 s, each placing the stream up to a second away from where the last one had it). FFmpeg places every stream by those reports, so the timestamps of such a camera step forward and back every few seconds, sound and picture at their own moments, while its frames and samples arrive complete and on time. Written as they came, every step forward was a gap and every step back was lost (a file cannot go back): a 30-minute segment of these cameras was 2 to 22 s longer than half an hour, had as many seconds of holes in its sound, and sound and picture were up to 5 s apart inside it. Now timestamps that step back while packets keep arriving are not taken for a new connection, and from then on that camera gets a gap in the file only where the arrival of its packets shows one: nothing for 2 s, or a step of whole frame intervals with the next frame late by at least half of it. Between the steps the camera's own timing is written as before, and a camera that never steps back is not touched. The log names such a camera once (`the source's timestamps step back and forth while its stream goes on`); `NOVA_TIMESTAMP_STEPS=keep` writes every step as it comes.
- Audio and video from the same camera share one clock inside each segment; the camera's audio is resampled when the encoder needs a different rate, and when its format changes after a reconnect.
- Whether a file gets an audio track is decided by the camera's first audio frame: the writer holds up to 2 s of video for it (not at all for a camera without an audio stream).

## Which encoder is used

Encoders are probed once per process, in this order, and the first that opens is used:

| output | preferred families |
|---|---|
| `.mp4` / `.mov` / `.mkv` | the camera's own codec family first (H.264 camera → H.264 file), then H.264, HEVC, AV1 |
| RTMP (`flv`) | H.264 only |
| HLS / MPEG-TS | H.264, HEVC |

Order: `-e` / `-c:v` (only if the output can carry that codec: `-e hevc_nvenc` is ignored with a warning for RTMP), then with `--accel` the matching hardware encoders (`cuda` → NVENC, `qsv` → Quick Sync, `vaapi` → the VAAPI encoders of a custom FFmpeg build, `videotoolbox`, AMF, and on Windows last the GPU's Media Foundation encoder: `h264_mf` with `hw_encoding=1`, at 0.1 bit per pixel and frame unless you give `-b:v`) for each allowed family, then `libx264` and the other software encoders (`libx265`, `libsvtav1`). Without `--accel` only software encoders are used. An encoder that passed the probe but does not open at the real size (NVENC's session limit, a GPU fault) is skipped for an hour and the writer continues with the next one, so no frame is lost (`X does not open at WxH; using Y`); an audio encoder that cannot open leaves the audio track out. A hardware encoder that rejects your `--ffmpeg` options is skipped with a warning (`h264_nvenc rejects your --ffmpeg options …`). An encoder you asked for with `-e` that does not open on this machine is named too (`-e hevc_nvenc does not open here (…); using libx264`): without that line a recording could go to the CPU unnoticed.

The recording keeps the camera's colours: its range (limited, or full as with `yuvj420p` cameras) and, where the camera names them, its matrix, primaries and transfer are written into the file, and the sample values are not rescaled. `--ffmpeg "-color_range tv"` and the like override that.

Defaults applied unless you override them: keyframe interval 2 s (`-g`), `hvc1` tag for HEVC in MP4, pixel format `nv12` for hardware encoders and `yuv420p` for software ones, and no B-frames for Quick Sync (`-bf 0`): its encoders work out each packet's decode time from the nominal frame rate, which does not fit a camera's real frame times. With `-bf 2` they still record (a decode time that would run backwards is moved, with one warning), at the price of a frame shown a few milliseconds off now and then.

## Tuning

All of these go into `--ffmpeg "…"` (details and the full table: [ffmpeg-options.md](ffmpeg-options.md)):

```bash
-b:v 4M -maxrate 4M -bufsize 8M     # capped bitrate (every encoder)
-crf 23 -preset veryfast            # libx264 quality mode
-rc vbr -cq 28 -preset p5           # NVENC quality mode
-global_quality 28                  # Quick Sync quality mode (a higher number: smaller file)
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
# Intel Quick Sync at about 2 Mbit/s, whatever the scene
python nova.py -i rtsp://... --accel qsv -r --ffmpeg "-b:v 2M"
```

### Quick Sync: how large the files get

Without a rate option the Quick Sync encoders keep the quantizer constant (FFmpeg says so at every start: `Using the constant quantization parameter (CQP) by default`). The quality is then fixed and the size follows the scene: one outdoor 1080p25 camera wrote 12.6 Mbit/s in the afternoon and 0.9 Mbit/s after dark, a quiet indoor 1080p10 camera 1.3 to 1.8 Mbit/s. For a size you can plan with, give a bitrate; for a constant but lower quality, a quality number. The same pictures through each setting at once (Intel UHD 620, November 2026):

| `--ffmpeg "…"` | Quick Sync mode | indoor 1080p10 | outdoor 1080p25, at night |
|---|---|---|---|
| nothing | constant quantizer (CQP) | 1.29 Mbit/s | 0.85 Mbit/s |
| `-global_quality 23` | constant quality (ICQ) | 1.11 | 3.06 |
| `-global_quality 26` | ICQ | 0.89 | 1.46 |
| `-global_quality 30` | ICQ | 0.67 | 0.63 |
| `-b:v 2M` | variable bitrate | 1.95 | 1.84 |
| `-b:v 4M` | variable bitrate | 3.94 | 3.77 |
| `-b:v 4M -maxrate 4M -bufsize 8M` | constant bitrate | 3.99 | 3.96 |
| `-e hevc_qsv` | CQP | 1.48 | 1.00 |
| `-e hevc_qsv --ffmpeg "-global_quality 26"` | ICQ | 0.98 | 0.71 |

A bitrate is used up even where the scene needs less (the indoor camera's own stream is 0.5 Mbit/s), so pick it per camera: `opts = --ffmpeg "-b:v 1M"` in that camera's section of `cameras.ini`, or in `[defaults]` for all of them.

## Disk usage and housekeeping

Size ≈ bitrate × time: 4 Mbit/s is about 1.8 GB per hour and 43 GB per day per camera; `-b:v`/`-cq`/`-global_quality` and `-r` are the levers. What you get without any of them depends on the encoder, by an order of magnitude. The same indoor 1080p camera at 10 fps, recorded with each one's defaults (November 2026):

| encoder | writes | CPU, decoding included |
|---|---|---|
| `h264_nvenc` (RTX 4090) | 11 to 12 Mbit/s | 5 % of a core |
| `h264_qsv` (Intel UHD 770 / 620) | 1.3 to 1.8 Mbit/s | 4 % / 8 % |
| `h264_vaapi` (Intel UHD 770, custom FFmpeg build) | 1.8 Mbit/s | 5 % |
| `h264_vaapi` (AMD Radeon of a Ryzen 9 9955HX, custom FFmpeg build) | 2.0 Mbit/s | 2 % |
| `libx264` | 2.0 Mbit/s | 47 % |
| `h264_amf` (AMD Radeon, Ryzen 5900HX) | 7.3 Mbit/s | 4 % |
| `h264_mf` (Windows Media Foundation) | 2.1 Mbit/s: Nova asks for 0.1 bit per pixel and frame, the encoder has no default | |

So on an NVIDIA machine `--ffmpeg "-b:v 2M"` cuts the disk use to a sixth (on an AMD one to a quarter), and `libx264` wants `-preset veryfast` when many cameras record on the CPU. Nova does not delete old recordings. A daily cron job keeps a rolling window:

```bash
# keep 14 days
0 3 * * * find /path/to/nova/videos -name '*.mp4' -mtime +14 -delete
```

On Windows, a scheduled task running `forfiles /P videos /S /M *.mp4 /D -14 /C "cmd /c del @path"` does the same.
