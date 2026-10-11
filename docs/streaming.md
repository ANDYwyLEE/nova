# Live streaming

- [How it works](#how-it-works)
- [YouTube (RTMP)](#youtube-rtmp)
- [YouTube HLS ingest (HEVC)](#youtube-hls-ingest-hevc)
- [Twitch and other RTMP servers](#twitch-and-other-rtmp-servers)
- [SRT, MPEG-TS over UDP, files](#srt-mpeg-ts-over-udp-files)
- [Bitrate and latency](#bitrate-and-latency)
- [Checklist when the stream does not show up](#checklist-when-the-stream-does-not-show-up)

## How it works

`-t TARGET` configures a live target; the stream starts immediately and **T** (F8, or the LIVE icon) toggles it. Streaming and recording run at the same time with separate encoders, and neither blocks the decoder or the window: each has its own thread and queue.

Nova adapts to the target on its own:

| target | container | video | audio |
|---|---|---|---|
| `rtmp://`, `rtmps://` | FLV | H.264 (NVENC / QSV / … / libx264) | AAC |
| `https://…m3u8` | HLS | H.264 or HEVC (`-e hevc_nvenc`, tagged `hvc1`) | AAC |
| `srt://`, `udp://`, `.ts` | MPEG-TS | H.264 or HEVC | AAC |
| `file.mp4` / `.mkv` | file | as for recording | AAC |

- Keyframe every 2 s (YouTube asks for ≤ 4 s).
- A camera without audio gets a **silent AAC track**: YouTube and most ingest servers refuse video-only streams. `--ffmpeg "-an"` disables that.
- Audio is sent only after the first video packet, so the ingest server sees video first.
- The connection and the encoder are set up on a worker thread; a slow or dead server never freezes the window. A broken connection stops the stream and turns the icon off; press **T** to retry.

## YouTube (RTMP)

1. YouTube Studio → **Go live** → **Stream**. Copy the *Stream key*.
2. Start Nova with the primary ingest URL and the key:

```bash
python nova.py -i rtsp://... -t rtmp://a.rtmp.youtube.com/live2/xxxx-xxxx-xxxx-xxxx-xxxx
# backup ingest (optional, second process or later retry)
python nova.py -i rtsp://... -t "rtmp://b.rtmp.youtube.com/live2?backup=1/xxxx-xxxx-xxxx-xxxx-xxxx"
```

In `cameras.ini`:

```ini
[lobby]
url = rtsp://viewer:password@192.168.1.13:554/av_stream/ch0
opts = -t rtmp://a.rtmp.youtube.com/live2/YOUR-STREAM-KEY --ffmpeg "-b:v 3M -maxrate 3M -bufsize 6M"
```

`nova start lobby` then streams with the window on screen; add `--headless` to the `opts` line (or run `nova start lobby --headless -t …`) to stream without one.

3. YouTube Studio shows "Excellent connection" within ~20 s; press **Go live** there if the stream is not set to start automatically.

Recommended bitrates (YouTube's own table, H.264): 1080p30 3–6 Mbit/s, 720p30 1.5–4 Mbit/s, 480p 0.5–2 Mbit/s. Without `-b:v`, NVENC and libx264 choose a quality-based rate that is usually within range for a static camera but can spike on motion; set `-b:v` + `-maxrate` for a steady upload.

Stream keys are secrets: keep them in `cameras.ini` (git-ignored). The pre-commit hook refuses commits that contain a `live2/…` key.

## YouTube HLS ingest (HEVC)

YouTube's HLS ingest accepts HEVC, which halves the bitrate for the same quality:

```bash
python nova.py -i rtsp://... --headless -e hevc_nvenc \
  -t "https://a.upload.youtube.com/http_upload_hls?cid=xxxx-xxxx-xxxx-xxxx-xxxx&copy=0&file=temp.m3u8" \
  --ffmpeg "-f hls -hls_time 2 -hls_list_size 4 -method PUT -vtag hvc1"
```

`cid` is the stream key. `-method PUT` is required by YouTube; `-hls_time 2` matches the keyframe interval.

## Twitch and other RTMP servers

```bash
python nova.py -i rtsp://... -t rtmp://live.twitch.tv/app/live_xxxxxxxxx     # Twitch (H.264, ≤ 6 Mbit/s)
python nova.py -i rtsp://... -t rtmp://192.168.1.7:1935/live/cam1           # nginx-rtmp, MediaMTX, OBS, …
```

Any RTMP server works. `--ffmpeg "-flvflags no_duration_filesize"` helps some servers that dislike the FLV header Nova writes for files.

## SRT, MPEG-TS over UDP, files

```bash
python nova.py -i rtsp://... -t "srt://relay.example.com:9000?mode=caller&passphrase=secret&pbkeylen=32"   # custom FFmpeg build only
python nova.py -i rtsp://... -t udp://239.0.0.1:5004                         # multicast MPEG-TS on the LAN
python nova.py -i rtsp://... -t udp://192.168.1.50:5004                      # unicast to a player (VLC: udp://@:5004)
python nova.py -i rtsp://... -t /mnt/nas/lobby.mkv                           # a file, with the live encoder settings
```

SRT options (`latency`, `mode`, `passphrase`, `pbkeylen`, `streamid`) go into `--ffmpeg` or the URL query. The PyPI wheel does not include SRT; see [custom-ffmpeg.md](custom-ffmpeg.md).

## Bitrate and latency

- Steady upload: `-b:v 3M -maxrate 3M -bufsize 6M` (constant-ish bitrate) instead of quality mode.
- Lower glass-to-glass latency: `-bf 0` (no B-frames), `-g 30`, NVENC `-preset p1 -tune ll`, libx264 `-tune zerolatency`.
- Save upload on a static scene: `-r 15` (drops frames, keeps audio).
- Several cameras on one uplink: spread encoders over GPUs with `NOVA_GPU_DEVICES=0,1`, and cap each camera's `-b:v`.

## Checklist when the stream does not show up

1. The log says `[AVStreamer] starting stream to: …` followed by `[AVWriter] h264_nvenc … -> flv audio=aac` (or `audio=silence`). If instead you see `worker stopped: …`, the connection failed: wrong key, blocked outbound port 1935, or DNS.
2. `EncoderManager … rejects your --ffmpeg options`: the chosen encoder does not understand an option (NVENC presets are `p1`–`p7`, libx264's are `ultrafast`–`veryslow`).
3. YouTube shows "no data": the key is for a different stream, or the stream was never started in Studio.
4. YouTube shows video but complains about audio: the camera sends G.711 at 8 kHz; Nova re-encodes it to AAC, which YouTube accepts, but `--ffmpeg "-ar 48000"` removes any doubt.
5. Choppy playback with a good uplink: raise `-maxrate`/`-bufsize`, or lower the resolution with `-s 1280x720`.
6. Everything else: run with `-l DEBUG` and look for `[AVWriter]` and `libav` lines.
