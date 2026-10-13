# Passing FFmpeg options to Nova (`--ffmpeg`)

Nova talks to FFmpeg through its libraries (libavformat, libavcodec, libavfilter), not by running the `ffmpeg` executable. That has one consequence worth understanding before reaching for `--ffmpeg`:

> Only **AVOptions** — options defined by a codec, a (de)muxer, a protocol or a filter — can take effect. Options implemented by the `ffmpeg` command-line program itself (`-t`, `-ss`, `-map`, `-re`, `-vsync`, `-y`, `-af`, `-filter_complex`, …) have no library equivalent. Nova reports them once (`... are ffmpeg command-line options without a library equivalent; ignored`) and drops them.

A handful of CLI conveniences are re-implemented by Nova itself because they are too useful to lose: `-vf`, `-r`, `-s`, `-pix_fmt`, `-ar`, `-ac`, `-metadata`.

Everything in the tables below was measured on 2026-10-11 with libx264 / h264_nvenc (encoding) and h264 / h264_cuvid (decoding) through Nova's own code path.

## How options are routed

`--ffmpeg "..."` is split with shell rules, then each `-key value` pair is classified by key name. Stream specifiers are understood: `-b:v` goes to the video encoder as `b`, `-b:a` to the audio encoder, `-c:v` selects the video encoder, `-tag:v` the codec tag.

| destination | examples | applied to |
|---|---|---|
| input / protocol | `rtsp_transport probesize analyzeduration fflags max_delay buffer_size timeout rw_timeout user_agent headers` | `av.open(url, options=…)`; `headers timeout rw_timeout tcp_nodelay rtmp_live rtmp_tcurl streamid` reach the camera input only, never an output |
| decoder | `threads skip_frame skip_loop_filter err_detect flags lowres resize surfaces deint drop_second_field gpu` | the decoder `CodecContext` |
| video encoder | `b maxrate bufsize crf preset tune profile level g bf refs rc cq qp x264-params …` and any unknown key | the encoder `CodecContext` |
| audio encoder | `b:a ar ac sample_fmt` and anything with `:a` | the audio `CodecContext` |
| container / muxer | `f movflags hls_* method segment_* flvflags mpegts_flags` | `av.open(target, "w", options=…)` |
| handled by Nova | `vf r s pix_fmt ar ac metadata an vtag c:v c:a` | see below |

Output *protocol* options (`rtmp_*`, `tcp_nodelay`, `rw_timeout`, SRT's `latency` `passphrase` …) cannot be set: PyAV opens the output connection with `avio_open2(…, NULL)`, so they never reach the protocol and are reported as "not used". SRT takes them in the URL query instead; the write timeout of a stream comes from `NOVA_TIMEOUT`.

Unknown keys default to the video encoder. After the encoder opens, keys it did not consume are reported:

```
WARN - [AVWriter] ⚠️ video encoder h264_nvenc ignored unknown --ffmpeg options: {'bogus_opt': '1'}
```

The same report exists for the decoder (`decoder h264_cuvid ignored unknown --ffmpeg options`).

## Input and decoder

| option | effect | notes |
|---|---|---|
| `-rtsp_transport tcp\|udp` | RTSP transport | default tcp |
| `-probesize N` `-analyzeduration N` | shorter stream analysis → faster connect | |
| `-fflags nobuffer` `-max_delay N` `-buffer_size N` `-reorder_queue_size N` | latency / buffering | `fflags nobuffer` also skips some initial frames |
| `-timeout N` `-rw_timeout N` | socket timeouts in µs | or `NOVA_TIMEOUT` env var |
| `-threads N` | decoder threads | software decoders |
| `-skip_frame nokey` | decode only keyframes | software decoders; cuvid accepts but ignores it. Cheap "one frame every GOP" preview |
| `-skip_loop_filter all` | faster, slightly blockier H.264 | software |
| `-err_detect ignore_err` `-flags low_delay` | tolerate broken streams / drop decoder delay | |
| `-resize WxH` | GPU-side downscale **inside the decoder** | **cuvid only** (`--accel cuda`). Reduces PCIe traffic and display/encode cost |
| `-surfaces N` `-deint adaptive` `-drop_second_field 1` | cuvid tuning | |
| `-lowres 1` | half-resolution decode | accepted, but **no effect for H.264/HEVC** (mjpeg, mpeg2 only) |
| `-hwaccel_output_format cuda` | — | no effect (Nova transfers frames to system memory) |

## Video encoder

Preset and tune **names differ per encoder**. When an encoder rejects your options Nova logs

```
WARN - [EncoderManager] ⚠️ h264_nvenc rejects your --ffmpeg options {'preset': 'veryfast'}: ... Trying the next encoder
```

and moves on (usually to libx264), which is probably not what you wanted.

| option | libx264 / libx265 | h264_nvenc / hevc_nvenc / av1_nvenc | notes |
|---|---|---|---|
| `-c:v NAME`, `-vcodec NAME` | ✓ | ✓ | same as `-e NAME`; ignored with a warning when the output cannot carry that codec (RTMP: H.264 only) |
| `-b:v 4M` | ✓ ABR | ✓ | measured: 1 Mbit/s requested → 1.2 / 0.94 Mbit/s |
| `-maxrate 4M -bufsize 8M` | ✓ (VBV) | ✓ | |
| `-crf 23` | ✓ | — (use `-cq`) | default libx264 quality is CRF 23 |
| `-rc vbr -cq 28` | — | ✓ | constant quality on NVENC |
| `-preset …` | `ultrafast … veryslow` | `p1` (fast) … `p7` (quality), `default`, `hq`, `ll` | |
| `-tune …` | `zerolatency film animation …` | `hq ll ull lossless` | |
| `-x264-params k=v:k=v` / `-x265-params` | ✓ | — | |
| `-g N` | ✓ | ✓ | keyframe interval in frames; **default 2 s** |
| `-bf 0` | ✓ | ✓ | no B-frames (lower latency) |
| `-profile:v baseline\|main\|high` | ✓ | ✓ | |
| `-level 3.1` | ✓ | ✓ | |
| `-refs N` `-flags +cgop` `-threads N` | ✓ | ✓/– | |
| `-pix_fmt yuv444p` | ✓ | ✓ | checked against the encoder's list; unsupported values are refused with a warning |
| `-s WxH` | ✓ | ✓ | output size (scaled before encoding) |
| `-r N` | ✓ | ✓ | output frame rate by **dropping** frames (never duplicates); audio unaffected |
| `-vf FILTERS` | ✓ | ✓ | libavfilter chain, see below |
| `-vtag hvc1\|hev1`, `-tag:v` | ✓ | ✓ | HEVC in mp4/HLS defaults to `hvc1` |
| `-multipass -spatial-aq 1 -temporal-aq 1 -rc-lookahead N` | — | ✓ | nvenc private options |

### `-vf` filters

The chain runs in libavfilter before encoding; the output size follows the filter result.

```bash
--ffmpeg "-vf transpose=1"                   # rotate 90° clockwise (2 = counter-clockwise, 3 = cw+flip)
--ffmpeg "-vf hflip"                         # mirror
--ffmpeg "-vf crop=1280:720:320:180"         # crop w:h:x:y
--ffmpeg "-vf scale=640:-2"                  # scale, keep aspect
--ffmpeg "-vf transpose=2,scale=-2:480 -r 5" # chain, plus frame decimation
```

Limits: frame-rate-changing filters (`fps`, `minterpolate`) are not supported — use `-r`. `drawtext` needs libfreetype, which the PyPI wheel lacks. A filter the build does not have fails loudly at start: `-vf: filter 'drawtext' is not available in this FFmpeg build`.

## Audio encoder

| option | effect |
|---|---|
| `-c:a aac\|libopus`, `-acodec` | encoder (default aac; opus is not allowed in flv/mp4) |
| `-b:a 64k` | bitrate |
| `-ar 16000` | sample rate — the camera audio is resampled |
| `-ac 1` | channels — downmix/upmix |
| `-an` | no audio track; also disables the silent track Nova adds to live streams |

## Container and muxer

| option | effect |
|---|---|
| `-f mpegts\|flv\|mp4\|matroska\|hls` | output format (otherwise derived from the target: rtmp→flv, srt/udp→mpegts, .m3u8→hls, extension) |
| `-movflags +faststart` | classic MP4 instead of the default `+frag_keyframe+empty_moov` |
| `-hls_time 2 -hls_list_size 4 -hls_flags … -method PUT` | HLS muxer / HTTP method (YouTube HLS ingest wants PUT) |
| `-metadata title=Cam1` | container metadata (repeatable) |
| `-flvflags no_duration_filesize` | FLV muxer (RTMP) |
| `srt://host:port?latency=200000&passphrase=…&pbkeylen=32` | SRT settings go in the URL, not `--ffmpeg` (custom FFmpeg build only) |
| `-mpegts_flags … -pat_period … -pcr_period …` | MPEG-TS |

## Not possible through the library

`-t -ss -to -y -n -map -re -readrate -vsync/-fps_mode -shortest -copyts -filter_complex -lavfi -af -frames -vframes -aframes -stream_loop -itsoffset -aspect`

Nova logs them once at start and ignores them. For `-t`-style limits use `--record-interval`; for `-af` there is no substitute yet.

## Recipes

```bash
# Low-bandwidth archive: 5 fps, 600 kbit/s, 2 s keyframes, mono 16 kHz
python nova.py -i rtsp://... -r --ffmpeg "-r 5 -b:v 600k -g 10 -ac 1 -ar 16000"

# Camera mounted on its side
python nova.py -i rtsp://... -r --ffmpeg "-vf transpose=1"

# Cheap preview of a 4K camera on a laptop: GPU downscale in the decoder (NVIDIA)
python nova.py -i rtsp://... --accel cuda --opengl --ffmpeg "-resize 960x540"

# Keyframe-only monitoring (one frame per GOP, almost no CPU)
python nova.py -i rtsp://... --ffmpeg "-skip_frame nokey"

# Lossy camera network: UDP transport, short probing
python nova.py -i rtsp://... --ffmpeg "-rtsp_transport udp -probesize 500000 -analyzeduration 0"

# YouTube RTMP with explicit NVENC quality settings
python nova.py -i rtsp://... -t rtmp://a.rtmp.youtube.com/live2/KEY --ffmpeg "-rc vbr -cq 26 -preset p5 -b:a 128k"

# YouTube HLS ingest, HEVC
python nova.py -i rtsp://... --headless -e hevc_nvenc \
  -t "https://a.upload.youtube.com/http_upload_hls?cid=KEY&copy=0&file=temp.m3u8" \
  --ffmpeg "-f hls -hls_time 2 -hls_list_size 4 -method PUT -vtag hvc1"

# Software-only machine, low CPU
python nova.py -i rtsp://... -r -e libx264 --ffmpeg "-preset ultrafast -crf 26 -threads 2"
```
