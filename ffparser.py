# ffparser.py
import re
import sys
import shlex
import argparse


class NovaParser(argparse.ArgumentParser):
    """argparse wrapper that lets '--ffmpeg "-rtsp_transport udp ..."' carry values starting with '-'."""
    def parse_args(self, args=None, namespace=None):
        if args is None:
            args = sys.argv[1:]
        new_args = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == '--ffmpeg' and i + 1 < len(args):
                new_args.append(arg)
                new_args.append(f" {args[i+1]}")
                i += 2
            else:
                new_args.append(arg)
                i += 1
        return super().parse_args(new_args, namespace)


class FFOpts:
    """Result of FFmpegParser.parse(): the user's '--ffmpeg' string split by destination."""
    def __init__(self):
        self.hw = {}        # HWAccel(options=...)             e.g. extra_hw_frames
        self.open = {}      # av.open(input, options=...)       e.g. rtsp_transport, probesize
        self.dec = {}       # decoder CodecContext.options      e.g. resize, lowres, gpu
        self.enc_v = {}     # video encoder CodecContext.options e.g. b, maxrate, preset, g, crf
        self.enc_a = {}     # audio encoder CodecContext.options e.g. b (from -b:a), ar, ac
        self.mux = {}       # av.open(output, options=...)      e.g. hls_time, movflags, method
        self.v_encoder = None   # -c:v / -vcodec
        self.a_encoder = None   # -c:a / -acodec
        self.v_tag = None       # -vtag / -tag:v
        self.no_audio = False   # -an
        self.fmt = None         # -f
        self.vf = None          # -vf / -filter:v  (libavfilter graph applied before encoding)
        self.metadata = {}      # -metadata key=value (container metadata)
        self.ignored = []       # ffmpeg-CLI-only options that have no libav equivalent

    def __iter__(self):
        # backward compatibility: hw, open, dec, enc_v, enc_a = FFmpegParser.parse(...)
        return iter((self.hw, self.open, self.dec, self.enc_v, self.enc_a))

    def __repr__(self):
        return (f"FFOpts(hw={self.hw}, open={self.open}, dec={self.dec}, enc_v={self.enc_v}, "
                f"enc_a={self.enc_a}, mux={self.mux}, v_encoder={self.v_encoder}, a_encoder={self.a_encoder}, "
                f"v_tag={self.v_tag}, no_audio={self.no_audio}, fmt={self.fmt}, vf={self.vf}, "
                f"metadata={self.metadata}, ignored={self.ignored})")


class FFmpegParser:
    # Keys are matched on the base name: '-b:v 4M' -> base 'b', stream 'v'.
    HW_KEYS = {
        'extra_hw_frames', 'hwaccel', 'init_hw_device', 'vsync',
    }
    # https://gist.github.com/tayvano/6e2d456a9897f55025e25035478a3a50
    OPEN_KEYS = {
        'allowed_media_types', 'analyzeduration', 'avoid_negative_ts', 'buffer_size',
        'correct_ts_overflow', 'cpucount', 'cpuflags', 'crypto', 'decrypt', 'decryption_key',
        'fflags', 'fifo_size', 'filter_buffered_frames', 'filter_hw_device', 'filter_threads',
        'formatprobesize', 'fpsprobesize', 'framesync', 'genpts', 'headers', 'http_persistent',
        'http_proxy', 'icy', 'icy_metadata_headers', 'icy_metadata_packet', 'initial_pause',
        'listen', 'location', 'max_alloc', 'max_delay', 'multiple_requests', 'offset',
        'overrun_nonfatal', 'password', 'probesize', 'program', 'protocol_whitelist',
        'recast_media', 'reconnect', 'reconnect_at_eof', 'reconnect_delay_max',
        'reconnect_on_http_error', 'reconnect_on_network_error', 'reconnect_streamed',
        'recv_buffer_size', 'reorder_queue_size', 'rtmp_live', 'rtmp_tcurl', 'rtpflags',
        'rtsp_flags', 'rtsp_transport', 'rw_timeout', 'scan_all_pmts', 'seekable',
        'skip_initial_bytes', 'stimeout', 'streamid', 'stream_group', 'stream_loop', 'target',
        'tcp_nodelay', 'timeout', 'ts_from_file', 'user', 'user_agent',
        # input device (v4l2/dshow/avfoundation) options
        'video_size', 'framerate', 'input_format', 'pixel_format', 'rtbufsize',
    }
    DEC_KEYS = {
        'cuvid_device', 'gpu', 'hwaccel_device', 'hwaccel_output_format', 'qsv_device',
        'vaapi_device', 'lowres', 'skip_frame', 'resize', 'crop', 'threads', 'thread_type',
        'skip_loop_filter', 'skip_idct', 'err_detect', 'drop_second_field', 'deint', 'surfaces',
        'flags', 'flags2', 'strict', 'async_depth', 'gpu_copy', 'extra_hw_frames',
    }
    # ffmpeg.c program options with no libav counterpart: reported once, then ignored
    CLI_ONLY_KEYS = {
        't', 'ss', 'to', 'y', 'n', 'map', 're', 'readrate', 'vsync', 'fps_mode', 'shortest',
        'copyts', 'start_at_zero', 'filter_complex', 'lavfi', 'af', 'frames', 'vframes', 'aframes',
        'dframes', 'stats', 'progress', 'loglevel', 'v', 'report', 'hide_banner', 'nostdin',
        'stream_loop', 'itsoffset', 'timestamp', 'aspect', 'vn', 'sn', 'dn', 'async', 'apad',
        'fflags+', 'benchmark', 'xerror', 'abort_on', 'max_error_rate', 'discard', 'disposition',
    }
    # Output container / muxer / output-protocol options
    MUX_KEYS = {
        'f', 'movflags', 'frag_duration', 'frag_size', 'min_frag_duration',
        'hls_time', 'hls_list_size', 'hls_playlist_type', 'hls_segment_filename',
        'hls_segment_type', 'hls_fmp4_init_filename', 'hls_flags', 'hls_delete_threshold',
        'hls_init_time', 'hls_base_url', 'master_pl_name',
        'segment_format', 'segment_time', 'segment_list', 'segment_list_size', 'segment_wrap',
        'strftime', 'start_number', 'max_muxing_queue_size', 'muxrate', 'packetsize',
        'method', 'http_persistent', 'http_user_agent', 'ignore_io_errors', 'headers',
        'flvflags', 'mpegts_flags', 'mpegts_copyts', 'pat_period', 'sdt_period', 'pcr_period',
        'rtmp_live', 'rtmp_buffer', 'rtmp_flashver', 'rtmp_app', 'rtmp_playpath', 'rtmp_tcurl',
        'tcp_nodelay', 'rw_timeout', 'timeout', 'latency', 'transtype', 'mode', 'passphrase',
        'pbkeylen', 'streamid', 'use_wallclock_as_timestamps', 'output_ts_offset',
        'metadata', 'map_metadata', 'copyts', 'avoid_negative_ts', 'y', 't',
    }
    # Keys valid on both sides that belong to the camera input only: an HTTP camera's -headers
    # ("Authorization: ...") must not reach an HLS ingest, and -timeout is in microseconds for RTSP but
    # in seconds for the HLS muxer (whose timeout Nova sets from NOVA_TIMEOUT).  Output protocol
    # options never reach the protocol anyway (docs/ffmpeg-options.md).
    INPUT_ONLY_KEYS = {
        'headers', 'timeout', 'rw_timeout', 'tcp_nodelay', 'rtmp_live', 'rtmp_tcurl', 'streamid',
    }
    # Video encoder CodecContext options (ffmpeg "-x" or "-x:v")
    ENC_KEYS = {
        'cuvid_device', 'gpu', 'hwaccel_device', 'hwaccel_output_format', 'qsv_device',
        'vaapi_device', 'allow_sw', 'async_depth', 'b', 'bf', 'bitrate', 'bits_per_raw_sample',
        'bufsize', 'b_qfactor', 'b_ref_mode', 'chroma_sample_location', 'color_range',
        'colorspace', 'color_primaries', 'color_trc', 'crf', 'cq', 'qp', 'delay', 'dn',
        'export_side_data', 'fast', 'field_order', 'flags', 'flags2', 'force_key_frames',
        'fpsmax', 'frame_threading', 'g', 'gop_size', 'keyint_min', 'gpu_copy', 'gray',
        'idct_algo', 'i_qfactor', 'level', 'maxrate', 'minrate', 'multipass', 'nvenc', 'pix_fmt',
        'pre', 'preset', 'profile', 'qmin', 'qmax', 'q', 'qscale', 'r', 'rc', 'rc-lookahead',
        'refs', 'reinit_filter', 's', 'scan_offset', 'sc_threshold', 'slice_threading',
        'strict_std_compliance', 'strict', 'surfaces', 'temporal-aq', 'spatial-aq', 'aq-strength',
        'thread_count', 'thread_type', 'threads', 'tune', 'look_ahead', 'look_ahead_depth',
        'low_power', 'x264-params', 'x265-params', 'svtav1-params', 'zerolatency', 'realtime',
        'quality', 'usage', 'rc_mode', 'global_quality', 'compression_level', 'workaround_bugs',
    }
    # Audio-only options (always routed to the audio encoder, even without ':a')
    AUDIO_KEYS = {'ac', 'ar', 'sample_fmt', 'channel_layout', 'ch_layout', 'aq', 'af', 'atag',
                  'cutoff', 'aac_coder', 'compression_level'}

    @staticmethod
    def parse(opt_str) -> FFOpts:
        o = FFOpts()
        if not opt_str or not opt_str.strip():
            return o

        tokens = shlex.split(opt_str.strip())
        parsed = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.startswith('-') and len(token) > 1 and not token[1].isdigit():
                key = token.lstrip('-')
                nxt = tokens[i + 1] if i + 1 < len(tokens) else None
                # a value may itself start with '-' only if it looks like a number (e.g. -crf -1)
                if nxt is not None and (not nxt.startswith('-') or re.match(r'^-\d', nxt)):
                    parsed.append((key, nxt)); i += 2
                else:
                    parsed.append((key, "1")); i += 1
            else:
                i += 1

        for key, value in parsed:
            v = str(value).strip()
            base, _, spec = key.partition(':')
            spec = spec.lower()

            # ---- codec selection / flags -------------------------------------------------
            if base in ('c', 'codec'):
                if spec == 'a': o.a_encoder = v
                else: o.v_encoder = v
                continue
            if base == 'vcodec': o.v_encoder = v; continue
            if base == 'acodec': o.a_encoder = v; continue
            if base in ('vtag',) or (base == 'tag' and spec in ('', 'v')):
                o.v_tag = v; continue
            if base == 'an': o.no_audio = True; continue
            if base == 'f': o.fmt = v; o.mux['f'] = v; continue
            if base == 'vf' or (base == 'filter' and spec in ('', 'v')):
                o.vf = v; continue
            if base == 'metadata':
                k, _, mv = v.partition('=')
                if k: o.metadata[k.strip()] = mv.strip()
                continue
            if base in FFmpegParser.CLI_ONLY_KEYS or (base == 'filter' and spec == 'a'):
                o.ignored.append(f"-{key}"); continue

            # ---- audio encoder -------------------------------------------------------------
            if spec == 'a' or base in FFmpegParser.AUDIO_KEYS:
                o.enc_a[base] = v
                continue

            matched = False
            if base in FFmpegParser.MUX_KEYS and base not in FFmpegParser.INPUT_ONLY_KEYS:
                o.mux[base] = v; matched = True
            if base in FFmpegParser.HW_KEYS:
                o.hw[base] = v; matched = True
            if base in FFmpegParser.OPEN_KEYS:
                o.open[base] = v; matched = True
            if base in FFmpegParser.DEC_KEYS:
                o.dec[base] = v; matched = True
            if base in FFmpegParser.ENC_KEYS or spec == 'v':
                o.enc_v[base] = v; matched = True
            if not matched:
                # Unknown key: assume a private video-encoder option (e.g. "-spatial_aq 1").
                o.enc_v[base] = v

        return o


if __name__ == "__main__":
    for s in [
        "-f hls -hls_time 2 -hls_list_size 4 -method PUT -vtag hvc1",
        "-rtsp_transport udp -resize 640x360 -b:v 4M -b:a 128k -g 60 -c:v hevc_nvenc -an",
        "-preset p4 -crf -1 -s 1280x720",
    ]:
        print(s, "\n  ->", FFmpegParser.parse(s))
