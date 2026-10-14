# Passing FFmpeg options to Nova (`--ffmpeg`)

Nova talks to FFmpeg through its libraries (libavformat, libavcodec, libavfilter, via PyAV), not by running the `ffmpeg` program. Two consequences are worth understanding before reaching for `--ffmpeg`:

> Only **AVOptions** can take effect: options that a codec, a (de)muxer, a protocol or a filter defines. Options of the `ffmpeg` program itself (`-t`, `-ss`, `-map`, `-re`, `-fps_mode`, `-q`, `-af`, `-filter_complex`, …) have no library equivalent. Nova reports them once, with a hint when it has something for the purpose, and drops them.

> PyAV does not expose everything the libraries can do; the list is in [What PyAV cannot pass on](#what-pyav-cannot-pass-on).

A handful of program options are re-implemented by Nova because they are too useful to lose: `-vf`, `-r`, `-s`, `-pix_fmt`, `-ar`, `-ac`, `-metadata`, `-an`, `-f`, `-c:v`, `-c:a`, `-tag:v`, `-hwaccel_device`.

Everything below was measured on 2026-10-14 with FFmpeg 9.0.2 (the FFmpeg bundled in the `av` 19.0.1 wheel) on an RTX 4090 with driver 595: software h264/hevc and h264_cuvid/hevc_cuvid decoders, libx264, libx265, h264_nvenc, hevc_nvenc, av1_nvenc, aac and libopus encoders, the mp4, flv, mpegts, hls and matroska muxers, through Nova's own code path.

## How options are routed

`--ffmpeg "..."` is split with shell rules; every `-key value` then goes **wherever FFmpeg defines an option of that name**. `ffparser.py` looks the name up in FFmpeg's own option tables: `ffopts_tables.py` is generated from `ffmpeg -h full` by `tools/gen_ffopts.py`, and the codec tables are read from the PyAV build in use at run time (so a [custom FFmpeg](custom-ffmpeg.md) brings its own options along).

| where FFmpeg defines the name | it goes to | examples |
|---|---|---|
| generic codec option, decoding parameter | the video decoder | `skip_frame skip_loop_filter skip_idct lowres err_detect extra_hw_frames apply_cropping discard_damaged_percentage` |
| generic codec option, encoding parameter | the video encoder (the audio encoder when the option is audio-only or given with `:a`) | `b maxrate bufsize g bf refs qmin qmax profile level colorspace color_range`; audio-only: `ab cutoff` |
| generic codec option, both | the decoder **and** the encoders | `threads thread_type flags flags2 strict err_detect max_pixels export_side_data` |
| a decoder's private option | the video decoder | cuvid `resize crop deint surfaces drop_second_field gpu`; qsv `async_depth gpu_copy`; libdav1d `tilethreads` |
| an encoder's private option | the video or the audio encoder | nvenc `cq rc preset tune multipass spatial-aq rc-lookahead b_ref_mode`; x264/x265 `crf preset tune x264-params x265-params`; aac `aac_coder`; libopus `application vbr frame_duration` |
| generic format option | the demuxer (decoding flag), the muxer (encoding flag) or both | `probesize analyzeduration use_wallclock_as_timestamps` / `avoid_negative_ts flush_packets max_interleave_delta output_ts_offset` / `fflags max_delay avioflags` |
| a demuxer's or an input protocol's private option | `av.open(url, options=…)` | `rtsp_transport rtsp_flags allowed_media_types reorder_queue_size buffer_size timeout user_agent`; `tcp_nodelay rw_timeout reconnect headers`; v4l2 `input_format` |
| a muxer's private option | `av.open(target, "w", options=…)` | `movflags frag_duration hls_* method flvflags mpegts_flags pcr_period segment_*` |
| a hardware-device option | `av_hwdevice_ctx_create()` (the `--accel` device) | cuda `primary_ctx`; vaapi `kernel_driver connection_type driver`; qsv `child_device child_device_type` |
| nothing Nova uses | the video encoder, with a warning | typos, options of codecs Nova never picks |

**Duplicates.** A name defined on several sides goes to every one of them: `-threads 2` limits the decoder and the encoders, `-flags +low_delay` reaches both, `-fflags +nobuffer` the demuxer and the muxer. That is what the `ffmpeg` program does with an option it finds in more than one class; the difference is that `ffmpeg` also knows *where* in the command line the option stood. Three names mean different things per side and are settled by hand: `timeout` and `headers` stay on the input (the HLS muxer's `timeout` is a duration, not microseconds, and an HTTP camera's `Authorization` header must not reach an ingest server), `method` goes to the output (the HTTP method of HLS uploads).

When the stream is open, Nova narrows the duplicates to the codec in use: the encoder gets only the options it defines itself, options of *other* encoders (`-cq` with libx264, `-crf` with nvenc, cuvid's `-resize` for a software decoder) are left out quietly, and a name no codec defines is passed through and reported if the codec does not take it either.

**`-i`.** Write the string like an `ffmpeg` command line and there is no doubt left: everything before `-i` is an input option (demuxer, protocol, decoder, hardware device), everything after it an output option (encoders, muxer). Before `-i`, `-f` is the input format and `-s`, `-r`, `-pix_fmt` are the capture-device options `video_size`, `framerate`, `pixel_format`, as in `ffmpeg`:

```bash
--ffmpeg "-rtsp_transport udp -threads 2 -i -c:v h264_nvenc -b:v 4M -threads 4"
--ffmpeg "-f v4l2 -s 1280x720 -r 30 -pix_fmt mjpeg -i -b:v 2M"      # a webcam, -i 0
```

A URL after `-i` is ignored with a note (the camera is Nova's own `-i`). An input option given after `-i` is still applied to the input, with a note in the log.

**Stream specifiers.** `-b:v` goes to the video encoder as `b`, `-b:a` to the audio encoder; anything with `:a` goes to the audio encoder. Without a specifier, audio-only options (`ab`, `ar`, `ac`, `cutoff`, `aac_*`, libopus options) find the audio encoder by themselves and options valid for both kinds (`b`, `profile`, `threads`, `compression_level`) go to the video side.

**What gets reported.** At start, in the log: program options (`-t is an option of the ffmpeg program, not of the FFmpeg libraries: use --record-interval; ignored`), options FFmpeg removed (`-stimeout`), names nobody defines (`passed to the video encoder, which will report it if it does not know it either`). When a codec opens: `decoder h264_cuvid ignored unknown --ffmpeg options: {…}` / `video encoder h264_nvenc ignored unknown --ffmpeg options: {…}`, for names no codec defines. When the output opens: PyAV's `Some options were not used: {…}` for muxer options the container does not have (`-hls_time` with an mp4). An encoder that *rejects* an option (an x264 preset name given to nvenc) is skipped with `EncoderManager ⚠️ h264_nvenc rejects your --ffmpeg options {…}` and the next encoder is tried, usually libx264, which is probably not what you wanted.

## What PyAV cannot pass on

- **Output protocol options** (`rtmp_live`, `rtmp_buffer`, `tcp_nodelay`, `rw_timeout`, SRT's `latency`, `passphrase`, `pbkeylen`, `streamid`, …): PyAV opens the output connection with `avio_open2(…, NULL)`, so no protocol option reaches an output. SRT takes them in the URL query (`srt://host:port?latency=200000&passphrase=…`); the write deadline of a stream comes from `NOVA_TIMEOUT`. Options of the *muxer* (`hls_*`, `movflags`, `flvflags`, `mpegts_*`) do arrive.
- **Input protocol options arrive**, with one exception: the RTSP demuxer opens its own connections and passes only its own `timeout`, `buffer_size` and `pkt_size` on, so `tcp_nodelay` and `rw_timeout` act on HTTP, TCP, UDP and SRT inputs but not on RTSP.
- **Hardware-device options** act on the hwaccel path only (`--accel vaapi`, `videotoolbox`, `d3d11va`, and `cuda` for codecs without a cuvid decoder). The dedicated decoders (`h264_cuvid`, `hevc_qsv`, …) create their context themselves; cuvid takes `gpu`.
- **Which GPU encodes** can be chosen for NVENC only (`gpu`, set from `--gpu`). The qsv, amf, vaapi and mf encoders take their device from a hardware frames context, which PyAV does not build; `-qsv_device` and `-vaapi_device` are options of the `ffmpeg` program.
- No bitstream filters (`-bsf`), no audio filters (`-af`; `-ar` and `-ac` are implemented), no scaler flags (`-sws_flags`), no two-pass encoding, no options for the audio *decoder*.
- Everything that is the `ffmpeg` program's: `-t -to -ss -map -re -readrate -fps_mode -vsync -shortest -copyts -itsoffset -q -qscale -frames -vframes -stream_loop -y -n -loglevel -hwaccel -hwaccel_output_format -init_hw_device -filter_complex -lavfi -max_muxing_queue_size -thread_queue_size -pass -sample_fmt -channel_layout -force_key_frames`.

## Input and decoder

| option | effect | notes |
|---|---|---|
| `-rtsp_transport tcp\|udp\|udp_multicast\|http` | RTSP transport | default tcp |
| `-rtsp_flags prefer_tcp\|filter_src` `-allowed_media_types video` | RTSP tuning; `video` skips the audio track altogether | |
| `-probesize N` `-analyzeduration N` | shorter stream analysis → faster connect | |
| `-fflags +nobuffer` `-max_delay N` `-buffer_size N` `-reorder_queue_size N` | latency / buffering | `nobuffer` also skips some initial frames |
| `-timeout N` `-rw_timeout N` | socket timeouts in µs | or `NOVA_TIMEOUT`; `-stimeout` was renamed `-timeout` in FFmpeg 5 |
| `-user_agent S` `-headers "K: v"` | RTSP / HTTP cameras | never forwarded to an output |
| `-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max N` | HTTP(S) input reconnect | Nova reconnects by itself anyway |
| `-use_wallclock_as_timestamps 1` | demuxer stamps packets with the wall clock | for cameras with broken timestamps |
| `-threads N` `-thread_type slice\|frame` | decoder threads | software decoders |
| `-skip_frame nokey` | decode only keyframes | software decoders; cuvid accepts but ignores it. Cheap "one frame every GOP" preview |
| `-skip_loop_filter all` `-skip_idct all` | faster, slightly blockier H.264 | software |
| `-err_detect ignore_err` `-flags +low_delay` `-discard_damaged_percentage N` | tolerate broken streams / drop decoder delay | |
| `-apply_cropping 0` | keep the coded size (H.264/HEVC cropping off) | |
| `-resize WxH` `-crop TxBxLxR` | GPU-side downscale / crop **inside the decoder** | **cuvid only** (`--accel cuda`). Reduces PCIe traffic and display/encode cost |
| `-surfaces N` `-deint weave\|bob\|adaptive` `-drop_second_field 1` | cuvid tuning | |
| `-lowres 1` | half-resolution decode | accepted, but **no effect for H.264/HEVC** (mjpeg, mpeg2 only) |
| `-extra_hw_frames N` | more frames in the hardware pool | hwaccel path |
| `-primary_ctx 1` `-kernel_driver i915` `-child_device /dev/dri/renderD129` | hardware-device creation | hwaccel path only, see above |

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
| `-crf 23` | ✓ | accepted, no effect (use `-cq`) | default libx264 quality is CRF 23 |
| `-rc vbr -cq 28` | `-cq` accepted, no effect | ✓ | constant quality on NVENC |
| `-preset …` | `ultrafast … veryslow` | `p1` (fast) … `p7` (quality), `default`, `hq`, `ll` | the wrong family is **rejected** (see above) |
| `-tune …` | `zerolatency film animation …` | `hq ll ull lossless` | rejected likewise |
| `-x264-params k=v:k=v` / `-x265-params` | ✓ | — | |
| `-g N` | ✓ | ✓ | keyframe interval in frames; **default 2 s** |
| `-bf 0` | ✓ | ✓ | no B-frames (lower latency) |
| `-profile:v baseline\|main\|high` | ✓ | ✓ | |
| `-level 4.1` | ✓ | ✓ | |
| `-refs N` `-flags +cgop` `-threads N` `-qmin/-qmax` `-qp N` | ✓ | ✓ | |
| `-sc_threshold 0` `-aq-mode N` `-rc-lookahead N` | ✓ | nvenc has `-no-scenecut 1`, `-spatial-aq`, `-rc-lookahead` | |
| `-pix_fmt yuv444p` | ✓ | ✓ | checked against the encoder's list; unsupported values are refused with a warning |
| `-s WxH` | ✓ | ✓ | output size (scaled before encoding) |
| `-r N` | ✓ | ✓ | output frame rate by **dropping** frames (never duplicates); audio unaffected |
| `-vf FILTERS` | ✓ | ✓ | libavfilter chain, see below |
| `-vtag hvc1\|hev1`, `-tag:v` | ✓ | ✓ | HEVC in mp4/HLS defaults to `hvc1` |
| `-color_range pc -colorspace bt709` | ✓ | ✓ | |
| `-multipass qres\|fullres -spatial-aq 1 -temporal-aq 1 -aq-strength N -rc-lookahead N -b_ref_mode middle -zerolatency 1 -forced-idr 1 -strict_gop 1 -intra-refresh 1 -surfaces N -delay 0 -lookahead_level N -ldkfs 1` | — | ✓ | nvenc private options (FFmpeg 7.1+ for `lookahead_level`, `ldkfs`) |
| `-tf_level 4` | — | refused here | NVENC temporal filtering: the driver would not open the encoder with it on this GPU |
| `-highbitdepth 1` | — | av1_nvenc | |
| `-svtav1-params k=v` `-crf N -preset 0…13` | libsvtav1 | — | do not mix `-b:v` with `-crf` (`Target Bitrate only supported when --rc is 1/2`) |

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
| `-b:a 64k`, `-ab 64k` | bitrate (AAC is capped at 6 × sample rate × channels bit/s) |
| `-ar 16000` | sample rate — the camera audio is resampled |
| `-ac 1` | channels — downmix/upmix |
| `-cutoff 8000` | low-pass before encoding (aac, libopus) |
| `-aac_coder fast -aac_pns 0 -profile:a aac_low` | aac private options |
| `-application lowdelay -vbr off -frame_duration 20 -packet_loss 10 -fec 1` | libopus private options |
| `-an` | no audio track; also disables the silent track Nova adds to live streams |

## Container and muxer

| option | effect |
|---|---|
| `-f mpegts\|flv\|mp4\|matroska\|hls` | output format (otherwise derived from the target: rtmp→flv, srt/udp→mpegts, .m3u8→hls, extension) |
| `-movflags +faststart` | classic MP4 instead of the default `+frag_keyframe+empty_moov` |
| `-frag_duration N` `-use_editlist 0` `-brand mp42` `-write_btrt 0` | mp4/mov muxer |
| `-hls_time 2 -hls_list_size 4 -hls_flags +independent_segments -hls_segment_type fmp4 -hls_delete_threshold N -http_persistent 1 -method PUT` | HLS muxer / HTTP method (YouTube HLS ingest wants PUT) |
| `-metadata title=Cam1` | container metadata (repeatable) |
| `-flvflags no_duration_filesize` | FLV muxer (RTMP) |
| `-mpegts_flags +resend_headers -pcr_period 20 -pat_period 0.1 -mpegts_copyts 1 -pes_payload_size N` | MPEG-TS |
| `-cluster_time_limit 1000 -live 1 -write_crc32 0` | Matroska |
| `-avoid_negative_ts make_zero -flush_packets 1 -max_interleave_delta 0 -output_ts_offset N` | generic muxer options |
| `srt://host:port?latency=200000&passphrase=…&pbkeylen=32` | SRT settings go in the URL, not `--ffmpeg` (custom FFmpeg build only) |

## Not possible through the library

`-t -to -ss -sseof -y -n -map -re -readrate -fps_mode -vsync -shortest -copyts -start_at_zero -itsoffset -filter_complex -lavfi -af -bsf -q -qscale -frames -vframes -aframes -stream_loop -pass -hwaccel -hwaccel_output_format -init_hw_device -vaapi_device -qsv_device -max_muxing_queue_size -thread_queue_size -sample_fmt -channel_layout -force_key_frames`

Nova logs them once at start, with the hint from the tables above, and ignores them.

## A custom FFmpeg

With a [custom build](custom-ffmpeg.md) the codec options are read from that build at run time, so new encoder options (a newer nvenc, `h264_vaapi`'s `rc_mode`, `low_power`, …) route without any change. The demuxer, muxer, protocol and program tables are generated; regenerate them from the new build when its major version differs from the one in `ffopts_tables.py`:

```bash
python tools/gen_ffopts.py --ffmpeg ~/deps/bin/ffmpeg --source ~/deps/ffmpeg > ffopts_tables.py
pytest tests/test_ffparser.py
```

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

# Decoder and encoder thread counts told apart, ffmpeg style
python nova.py -i rtsp://... -r --ffmpeg "-threads 1 -i -threads 4 -preset veryfast"

# A USB webcam at 720p30 in MJPEG (the device is -i 0)
python nova.py -i 0 --ffmpeg "-f v4l2 -input_format mjpeg -s 1280x720 -r 30"

# YouTube RTMP with explicit NVENC quality settings
python nova.py -i rtsp://... -t rtmp://a.rtmp.youtube.com/live2/KEY --ffmpeg "-rc vbr -cq 26 -preset p5 -b:a 128k"

# YouTube HLS ingest, HEVC
python nova.py -i rtsp://... --headless -e hevc_nvenc \
  -t "https://a.upload.youtube.com/http_upload_hls?cid=KEY&copy=0&file=temp.m3u8" \
  --ffmpeg "-f hls -hls_time 2 -hls_list_size 4 -method PUT -vtag hvc1"

# Software-only machine, low CPU
python nova.py -i rtsp://... -r -e libx264 --ffmpeg "-preset ultrafast -crf 26 -threads 2"
```
