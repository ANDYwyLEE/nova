# ffparser.py
import re
import argparse

# fparser.py
import argparse
import sys
import re
import shlex


class NovaParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        if args is None:
            args = sys.argv[1:]
            #print(f"{args}")
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



class FFmpegParser:
    RAW_HW_KEYS = {
        'extra_hw_frames',
        'hwaccel',
        'init_hw_device',
        'vsync',
    }
    # https://gist.github.com/tayvano/6e2d456a9897f55025e25035478a3a50
    RAW_OPEN_KEYS = {
        'allowed_media_types',
        'analyzeduration',
        'avoid_negative_ts',
        'buffer_size',
        'correct_ts_overflow',
        'cpucount',
        'cpuflags',
        'crypto',
        'decrypt',
        'decryption_key',
        'fflags',
        'fifo_size',
        'filter_buffered_frames',
        'filter_hw_device',
        'filter_threads',
        'formatprobesize',
        'fpsprobesize',
        'framesync',
        'genpts',
        'headers',
        'http_persistent',
        'http_proxy',
        'icy',
        'icy_metadata_headers',
        'icy_metadata_packet',
        'initial_pause',
        'listen',
        'location',
        'max_alloc',
        'max_delay',
        'multiple_requests',
        'offset',
        'overrun_nonfatal',
        'password',
        'probesize',
        'program',
        'protocol_whitelist',
        'recast_media',
        'reconnect',
        'reconnect_at_eof',
        'reconnect_delay_max',
        'reconnect_on_http_error',
        'reconnect_on_network_error',
        'reconnect_streamed',
        'recv_buffer_size',
        'reorder_queue_size',
        'rtmp_live',
        'rtmp_tcurl',
        'rtpflags',
        'rtsp_flags',
        'rtsp_transport',
        'rw_timeout',
        'scan_all_pmts',
        'seekable',
        'skip_initial_bytes',
        'stimeout',
        'streamid',
        'stream_group',
        'stream_loop',
        'target',
        'tcp_nodelay',
        'timeout',
        'ts_from_file',
        'user',
        'user_agent',
    }

    RAW_DEC_KEYS = {
        'cuvid_device',
        r'^gpu(?::\S+)?$',
        'hwaccel_device',
        'hwaccel_output_format',
        'qsv_device',
        're',
        'vaapi_device',
        #r'^(\S+)_device$',
        'lowres',
        'skipframe', 'skip_frame',
        'resize',
        r'^threads(?::\S+)?$',
    }

    RAW_ENC_KEYS = {
        'cuvid_device',
        r'^gpu(?::\S+)?$',
        'hwaccel_device',
        'hwaccel_output_format',
        'qsv_device',
        'vaapi_device',
        #r'^(\S+)_device$',
        r'^ac(?::\S+)?$',
        'acodec',
        'af',
        'allow_sw',
        'an',
        'aq',
        'atag',
        r'^ar(?::\S+)?$',
        r'^apply_cropping(?::\S+)?$',
        r'^aspect(?::\S+)?$',
        'async_depth',
        'avoid_negative_ts',
        'b',
        'bf',
        'bitrate',
        'bits_per_raw_sample',
        'bug',
        r'^b(?::\S+)?$',
        r'^bufsize(?::\S+)?$',
        'b_qfactor',
        'b_ref_mode',
        r'^c(?::\S+)?$',
        'channel_layout',
        r'^ch_layout(?::\S+)?$',
        'chroma_sample_location',
        r'^codec(?::\S+)?$',
        'color_range',
        'colorspace',
        'copyts',
        'copy_unknown',
        'crf',
        'deint',
        'delay',
        r'^display_hflip(?::\S+)?$',
        r'^display_rotation(?::\S+)?$',
        r'^display_vflip(?::\S+)?$',
        'dn',
        r'^disposition(?::\S+)?$',
        'drc_scale',
        r'^drop_changed(?::\S+)?$',
        'drop_second_field',
        'ec',
        'err_detect',
        'export_side_data',
        'f',
        'fast',
        'field_order',
        r'^filter(?::\S+)?$',
        'filter_complex',
        'flags',
        'flags2',
        r'^force_key_frames(?::\S+)?$',
        r'^fpsmax(?::\S+)?$', # (Hz value)
        'framerate',
        'frame_threading',
        'g',
        'gpu_copy',
        'gray',
        'hls_fmp4_init_filename',
        'hls_list_size',
        'hls_playlist_type',
        'hls_segment_filename',
        'hls_segment_type',
        'hls_time',
        'idct_algo',
        'ignore_unknown',
        'i_qfactor',
        r'^keyint_min(?::\S+)?$',
        r'^level(?::\S+)?$',
        'map',
        'map_chapters',
        r'^map_metadata(?::\S+)?$',
        'maxrate',
        'max_muxing_queue_size',
        r'^metadata(?::\S+)?$',
        'minrate',
        'movflags',
        'maxrate',
        'muxrate',
        r'^nvenc-(?::\S+)?$',
        'output_format',
        'packetsize',
        r'^pix_fmt(?::\S+)?$',
        'pixel_format',
        r'^pre(?::\S+)?$',
        'preset',
        r'^profile(?::\S+)?$',
        'qmin',
        r'^q(?::\S+)?$',
        r'^qscale(?::\S+)?$',
        r'^r(?::\S+)?$', # (Hz value)
        'rc-lookahead',
        're',
        'readrate',
        'refcounted_frames',
        r'^reinit_filter(?::\S+)?$',
        r'^s(?::\S+)?$', # wxh
        'scan_offset',
        'scodec',
        'segment_format',
        'segment_time',
        'skip_idct',
        'skip_loop_filter',
        'slice_threading',
        'sn',
        'source_timebase',
        'start_number',
        'strftime',
        'strict_std_compliance',
        'surfaces',
        't',
        r'^tag(?::\S+)?$',
        'temporal-aq',
        'thread_count',
        'thread_type',
        'tune',
        'vcodec',
        'vdpau_render',
        'vf',
        'vn',
        'workaround_bugs',
        r'^x264-(?::\S+)?$',
        r'^x265-(?::\S+)?$',
        'y',
        'zerocopy',
    }

    RAW_AUDIO_KEYS = {
        r'^ac(?::\S+)?$',
        'acodec',
        'af',
        'an',
        'aq',
        'atag',
        r'^ar(?::\S+)?$',
        'channel_layout',
        r'^ch_layout(?::\S+)?$',
        r'^sample_fmt(?::\S+)?$',
    }

    HW_KEYS = set()
    HW_PATTERNS = []
    OPEN_KEYS = set()
    OPEN_PATTERNS = []
    DEC_KEYS = set()
    DEC_PATTERNS = []
    ENC_KEYS = set()
    ENC_PATTERNS = []
    AUDIO_KEYS = set()
    AUDIO_PATTERNS = []

    @classmethod
    def _initialize_filters(cls):
        for k in cls.RAW_HW_KEYS:
            if k.startswith('^'):
                cls.HW_PATTERNS.append(re.compile(k))
            else:
                cls.HW_KEYS.add(k)
        for k in cls.RAW_OPEN_KEYS:
            if k.startswith('^'):
                cls.OPEN_PATTERNS.append(re.compile(k))
            else:
                cls.OPEN_KEYS.add(k)
        for k in cls.RAW_DEC_KEYS:
            if k.startswith('^'):
                cls.DEC_PATTERNS.append(re.compile(k))
            else:
                cls.DEC_KEYS.add(k)
        for k in cls.RAW_ENC_KEYS:
            if k.startswith('^'):
                cls.ENC_PATTERNS.append(re.compile(k))
            else:
                cls.ENC_KEYS.add(k)
        for k in cls.RAW_AUDIO_KEYS:
            if k.startswith('^'):
                cls.AUDIO_PATTERNS.append(re.compile(k))
            else:
                cls.AUDIO_KEYS.add(k)

    @classmethod
    def _is_match(cls, key, key_set, pattern_list):
        if key in key_set: return True
        return any(p.match(key) for p in pattern_list)

    @staticmethod
    def parse(opt_str):
        if not hasattr(FFmpegParser, '_initialized'):
            FFmpegParser._initialize_filters()
            FFmpegParser._initialized = True
        if not opt_str: return {}, {}, {}, {}, {}

        tokens = shlex.split(opt_str.strip())
        parsed_dict = {}
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.startswith('-'):
                key = token.lstrip('-')
                if i + 1 < len(tokens) and not tokens[i+1].startswith('-'):
                    value = tokens[i+1]
                    i += 2
                else:
                    value = "1"
                    i += 1
                parsed_dict[key] = value
            else:
                i += 1


        hw_opts = {}
        open_opts = {}
        dec_opts = {}
        enc_opts = {}
        audio_opts = {}

        for key, value in parsed_dict.items():
            v = str(value.strip())
            if ':a' in key or FFmpegParser._is_match(key, FFmpegParser.AUDIO_KEYS, FFmpegParser.AUDIO_PATTERNS):
                audio_opts[key] = v
                if not (':v' in key or key in ['b', 'q', 'c']): continue
            if FFmpegParser._is_match(key, FFmpegParser.ENC_KEYS, FFmpegParser.ENC_PATTERNS):
                if not (':a' in key): enc_opts[key] = v
            if FFmpegParser._is_match(key, FFmpegParser.HW_KEYS, FFmpegParser.HW_PATTERNS):
                hw_opts[key] = v
            if FFmpegParser._is_match(key, FFmpegParser.OPEN_KEYS, FFmpegParser.OPEN_PATTERNS):
                open_opts[key] = v
            if FFmpegParser._is_match(key, FFmpegParser.DEC_KEYS, FFmpegParser.DEC_PATTERNS):
                dec_opts[key] = v

        return hw_opts, open_opts, dec_opts, enc_opts, audio_opts
