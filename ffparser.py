# ffparser.py
"""--ffmpeg "..." -> FFOpts: every "-key value" goes where FFmpeg defines an option of that name.

Nova drives FFmpeg through its libraries (PyAV), not through the ffmpeg program, so an option can
only take effect where libav has an AVOption of that name:

  * the generic codec context (shared by every decoder and encoder): the option's own flags say
    whether it is a decoding or an encoding parameter and whether it is for video or audio;
  * the private options of a decoder or of an encoder;
  * the generic format context (demuxer side or muxer side, again by flags);
  * the private options of a demuxer, a muxer or an input protocol;
  * the hardware-device constructor (av_hwdevice_ctx_create).

ffopts_tables.py, generated from FFmpeg's own tables by tools/gen_ffopts.py, says which is which.
At run time the codec tables are read from the PyAV build in use when it can list them (av >= 19:
CodecContext.supported_options), so the private options of a custom FFmpeg are known too.

A name defined on more than one side goes to every side that defines it ("-threads" and
"-flags" to the decoder and the encoders, "-fflags" to the demuxer and the muxer), which is
what the ffmpeg program does with an option it finds in both classes; the few names whose
meaning differs per side are settled by PREFER.  Writing the string like an ffmpeg command line,
with "-i" between the input and the output options, removes any doubt:

    --ffmpeg "-rtsp_transport udp -threads 2 -i -c:v h264_nvenc -b:v 4M -threads 4"

Options of the ffmpeg program itself (-t, -map, -re, -fps_mode, ...) have no library equivalent:
they are reported once, with a hint when Nova has something for the purpose, and dropped.  A few
are re-implemented by Nova because they are too useful to lose: -vf, -r, -s, -pix_fmt, -ar, -ac,
-metadata, -an, -f, -c:v, -c:a, -tag:v, -hwaccel_device.
"""
import re
import sys
import shlex
import argparse

import ffopts_tables as T


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
        self.hw = {}        # HWAccel(options=...): av_hwdevice_ctx_create() options (cuda primary_ctx, vaapi kernel_driver ...)
        self.open = {}      # av.open(input, options=...): demuxer, input protocol and generic demuxing options
        self.dec = {}       # the video decoder's CodecContext.options
        self.enc_v = {}     # the video encoder's CodecContext.options
        self.enc_a = {}     # the audio encoder's (-x:a, audio-only options, -ar, -ac)
        self.mux = {}       # av.open(output, "w", options=...): muxer and generic muxing options
        self.v_encoder = None   # -c:v / -vcodec
        self.a_encoder = None   # -c:a / -acodec
        self.v_tag = None       # -vtag / -tag:v
        self.no_audio = False   # -an
        self.fmt = None         # -f (after -i, or without -i): output container
        self.in_fmt = None      # -f before -i: input format (v4l2, dshow, mjpeg ...)
        self.vf = None          # -vf / -filter:v  (libavfilter graph applied before encoding)
        self.metadata = {}      # -metadata key=value (container metadata)
        self.hw_device = None   # -hwaccel_device: device index or path for --accel (instead of --gpu)
        self.ignored = []       # options dropped: ffmpeg program options, options FFmpeg removed
        self.warnings = []      # what to log at WARNING, one line each (dropped options with hints, unknown names)
        self.notes = []         # what to log at INFO (routing decisions worth knowing)

    def __iter__(self):
        # backward compatibility: hw, open, dec, enc_v, enc_a = FFmpegParser.parse(...)
        return iter((self.hw, self.open, self.dec, self.enc_v, self.enc_a))

    def __repr__(self):
        return (f"FFOpts(hw={self.hw}, open={self.open}, dec={self.dec}, enc_v={self.enc_v}, "
                f"enc_a={self.enc_a}, mux={self.mux}, v_encoder={self.v_encoder}, a_encoder={self.a_encoder}, "
                f"v_tag={self.v_tag}, no_audio={self.no_audio}, fmt={self.fmt}, in_fmt={self.in_fmt}, vf={self.vf}, "
                f"metadata={self.metadata}, hw_device={self.hw_device}, ignored={self.ignored})")


IN_SIDES = frozenset(("open", "dec", "hw"))
OUT_SIDES = frozenset(("enc_v", "enc_a", "mux"))

# Names defined on both the input and the output side whose meaning differs per side.  The other
# such names (http_persistent, start_number: hls demuxer/muxer, image2) mean the same on both.
PREFER = {
    'timeout': 'in',          # rtsp/tcp/udp/srt: socket timeout in us; the HLS muxer's (a duration) comes from NOVA_TIMEOUT
    'headers': 'in',          # an HTTP camera's Authorization header must not reach an HLS ingest
    'method': 'out',          # HLS muxer: HTTP method of the uploads (YouTube wants PUT); never an HTTP camera's request
}

# ffmpeg program options Nova users may reach for, with what to use instead.
CLI_HINTS = {
    't': "use --record-interval", 'to': "use --record-interval",
    'frames': "use --record-interval", 'vframes': "use --record-interval", 'aframes': "use --record-interval",
    'ss': "a live camera cannot be seeked", 'sseof': "a live camera cannot be seeked",
    're': "Nova reads the camera in real time anyway", 'readrate': "Nova reads the camera in real time anyway",
    'stream_loop': "Nova reads the camera in real time anyway",
    'map': "Nova writes the camera's video and audio only", 'vn': "Nova needs the video",
    'sn': "Nova writes no subtitle streams anyway", 'dn': "Nova writes no data streams anyway",
    'af': "audio filters are not supported (-ar and -ac are)",
    'filter_complex': "use -vf", 'lavfi': "use -vf",
    'fps_mode': "use -r N (frames are dropped, never duplicated)", 'vsync': "use -r N (frames are dropped, never duplicated)",
    'force_key_frames': "use -g N (keyframe interval in frames)",
    'hwaccel': "use --accel", 'hwaccel_output_format': "Nova copies decoded frames to system memory",
    'init_hw_device': "use --gpu N or -hwaccel_device", 'vaapi_device': "use --gpu N or -hwaccel_device",
    'qsv_device': "use --gpu N or -hwaccel_device", 'filter_hw_device': "use --gpu N or -hwaccel_device",
    'q': "use -crf (libx264/libx265), -cq (nvenc) or -global_quality (qsv)",
    'qscale': "use -crf (libx264/libx265), -cq (nvenc) or -global_quality (qsv)",
    'aq': "use -b:a", 'vq': "use -crf (libx264/libx265), -cq (nvenc) or -global_quality (qsv)",
    'bsf': "bitstream filters are not supported",
    'loglevel': "use -l LEVEL", 'v': "use -l LEVEL", 'report': "use -l DEBUG",
    'y': "Nova never overwrites a recording", 'n': "Nova never overwrites a recording",
    'shortest': "recordings end with --record-interval or stop",
    'pass': "a live source cannot be encoded twice",
    'copyts': "timestamps are set by Nova (docs/recording.md)", 'start_at_zero': "timestamps are set by Nova (docs/recording.md)",
    'itsoffset': "timestamps are set by Nova (docs/recording.md)",
    'max_muxing_queue_size': "the writer queues are Nova's own", 'thread_queue_size': "the reader queue is Nova's own",
    'sample_fmt': "Nova converts the audio to the encoder's sample format",
    'channel_layout': "use -ac N", 'atag': "audio codec tags are not supported (-tag:v is)",
    'autorotate': "use -vf transpose=N", 'display_rotation': "use -vf transpose=N",
}

# Options FFmpeg has removed; they would otherwise be taken for an encoder's private option.
REMOVED = {
    'stimeout': "renamed -timeout (microseconds) in FFmpeg 5; Nova sets it from NOVA_TIMEOUT",
    'vsync': "removed in FFmpeg 9; use -r N",
    'rtmp_tcurl': "an output protocol option; PyAV cannot pass those (docs/ffmpeg-options.md)",
}

# Sides a hint refers to, for the message when an option sits on the wrong side of -i.
_SIDE_NAME = {'in': "input", 'out': "output"}

_FAMILIES = ("h264", "hevc", "mjpeg", "mpeg4", "mpeg2video", "av1", "vp8", "vp9", "vc1")
_DEC_SUFFIXES = ("cuvid", "qsv", "v4l2m2m", "rkmpp", "mediacodec", "amf")
_ENC_SUFFIXES = ("nvenc", "qsv", "amf", "vaapi", "videotoolbox", "v4l2m2m", "mf", "rkmpp", "mediacodec")
_SW_DECODERS = ("libdav1d",)
_SW_ENCODERS = ("libx264", "libx265", "libsvtav1", "libaom-av1", "mjpeg",
                "aac", "libopus", "libmp3lame", "libfdk_aac", "aac_mf", "aac_at")
_HWDEVICE_NAMES = frozenset(n for names in T.HWDEVICE.values() for n in names)

# AV_OPT_FLAG_* bits (libavutil/opt.h)
_FLAG_BITS = ((1, 'E'), (2, 'D'), (16, 'V'), (8, 'A'))


def _flags_str(flags) -> str:
    f = int(flags)
    return "".join(ch for bit, ch in _FLAG_BITS if f & bit)


def _merge(dst, name, flags, owner):
    fl, owners = dst.get(name, ("", ()))
    dst[name] = ("".join(c for c in "EDVA" if c in fl or c in flags), tuple(sorted(set(owners) | {owner})))


class _Index:
    """Codec option tables: generic {name: flags}, dec/enc {name: (flags, owners)}."""
    def __init__(self, generic, dec, enc, source):
        self.generic, self.dec, self.enc, self.source = generic, dec, enc, source


def _candidate_codecs():
    dec = list(_FAMILIES) + list(_SW_DECODERS) + [
        f"{c.replace('mpeg2video', 'mpeg2')}_{s}" for s in _DEC_SUFFIXES for c in _FAMILIES]
    enc = list(_SW_ENCODERS) + [f"{c}_{s}" for s in _ENC_SUFFIXES for c in ("h264", "hevc", "av1")] \
        + ["mjpeg_qsv", "mjpeg_vaapi"]
    return dec, enc


def _index_from_pyav():
    """The codec tables of the PyAV build in use, or None when it cannot list them."""
    try:
        import av
        import av.logging
        from av.codec.context import CodecContext
        if not hasattr(CodecContext, "supported_options"):
            return None
        available = set(av.codecs_available)
    except Exception:
        return None
    generic, dec, enc = {}, {}, {}
    dec_names, enc_names = _candidate_codecs()
    # Reading an option's default makes FFmpeg warn about every deprecated option ("The "dc" option
    # is deprecated", dozens of lines): keep its log quiet while the tables are read.  get_level()
    # is None by default (no threshold), which set_level() takes back.
    try:
        level, quiet = av.logging.get_level(), True
        av.logging.set_level(av.logging.FATAL)
    except Exception:
        level, quiet = None, False
    try:
        for names, mode, dst in ((dec_names, 'r', dec), (enc_names, 'w', enc)):
            for name in names:
                if name not in available:
                    continue
                try:
                    so = CodecContext.create(name, mode).supported_options
                except Exception:
                    continue
                if not generic:
                    generic = {o.name: _flags_str(o.flags) for o in so.generic}
                for o in so.private:
                    _merge(dst, o.name, _flags_str(o.flags), name)
    finally:
        if quiet:
            try: av.logging.set_level(level)
            except Exception: pass
    if not generic:
        return None
    v = ".".join(str(x) for x in av.library_versions.get("libavcodec", ()))
    return _Index(generic, dec, enc, f"PyAV {av.__version__} (libavcodec {v})")


def _static_index() -> _Index:
    return _Index(dict(T.CODEC_GENERIC), dict(T.DECODER_PRIVATE), dict(T.ENCODER_PRIVATE),
                  f"tables of FFmpeg {T.FFMPEG_VERSION}")


_index = None


def codec_index() -> _Index:
    """The generated tables with the PyAV build's own on top: the tables know the codecs of every
    platform (cuvid's -resize is a decoder option on a Mac too, where it is then left out for the
    decoder in use), the build adds what its FFmpeg has beyond them."""
    global _index
    if _index is None:
        idx = _static_index()
        live = _index_from_pyav()
        if live is not None:
            for name, fl in live.generic.items():
                idx.generic[name] = "".join(c for c in "EDVA" if c in idx.generic.get(name, "") or c in fl)
            for src, dst in ((live.dec, idx.dec), (live.enc, idx.enc)):
                for name, (fl, owners_) in src.items():
                    for o in owners_:
                        _merge(dst, name, fl, o)
            idx.source = f"{live.source} + {idx.source}"
        _index = idx
    return _index


def _audio_only(flags: str) -> bool:
    return 'A' in flags and 'V' not in flags


def destinations(base: str, spec: str = "") -> set:
    """Where FFmpeg defines an option named base, as a subset of {'hw', 'open', 'dec', 'enc_v',
    'enc_a', 'mux'}; empty when no component Nova uses has it.  spec is the stream specifier
    ('v', 'a' or '')."""
    idx = codec_index()
    d = set()
    fl = idx.generic.get(base)
    if fl:
        if 'D' in fl and spec != 'a' and not _audio_only(fl):
            d.add('dec')
        if 'E' in fl:
            d.add('enc_a' if (spec == 'a' or _audio_only(fl)) else 'enc_v')
    if base in idx.dec and spec != 'a':
        d.add('dec')
    p = idx.enc.get(base)
    if p is not None:
        d.add('enc_a' if (spec == 'a' or _audio_only(p[0])) else 'enc_v')
    fl = T.FORMAT_GENERIC.get(base, "")
    if 'D' in fl:
        d.add('open')
    if 'E' in fl:
        d.add('mux')
    if base in T.DEMUXER_PRIVATE or base in T.PROTOCOL:
        d.add('open')
    if base in T.MUXER_PRIVATE:
        d.add('mux')
    if base in _HWDEVICE_NAMES:
        d.add('hw')
    pref = PREFER.get(base)
    if pref == 'in':
        d -= OUT_SIDES
    elif pref == 'out':
        d -= IN_SIDES
    return d


def owners(base: str) -> list:
    """Components that define an option of that name (for messages)."""
    idx = codec_index()
    out = []
    for table in (idx.dec, idx.enc, T.DEMUXER_PRIVATE, T.MUXER_PRIVATE, T.PROTOCOL):
        if base in table:
            out.extend(table[base][1])
    if base in idx.generic:
        out.append("every codec")
    if base in T.FORMAT_GENERIC:
        out.append("every container")
    return out


def for_codec(ctx, opts: dict, what: str = "codec"):
    """(apply, skipped): the options of opts this codec context defines, and those meant for another
    codec (a private option of some other decoder or encoder, '-cq' for libx264 or '-resize' for a
    software decoder), which are left out quietly so that the real "ignored unknown option"
    report stays meaningful.  A name no codec defines is applied and left for that report.
    Without CodecContext.supported_options (av < 19) everything is applied."""
    if not opts:
        return dict(opts), {}
    try:
        so = ctx.supported_options
        known = {o.name for o in so.generic} | {o.name for o in so.private}
    except Exception:
        return dict(opts), {}
    idx = codec_index()
    apply, skipped = {}, {}
    for k, v in opts.items():
        if k in known or (k not in idx.dec and k not in idx.enc):
            apply[k] = v
        else:
            skipped[k] = v
    return apply, skipped


from util import mask_url as _mask


class FFmpegParser:
    @staticmethod
    def parse(opt_str) -> FFOpts:
        o = FFOpts()
        if not opt_str or not opt_str.strip():
            return o

        try:
            tokens = shlex.split(opt_str.strip())
        except ValueError as e:                               # unbalanced quotes
            o.warnings.append(f"cannot read {opt_str!r} ({e}); the options are ignored")
            return o
        parsed = []                   # (key, value, side): side None until an -i is seen
        saw_i = False
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token == '-i':
                if saw_i:
                    o.notes.append("-i given twice; everything after the first one is an output option")
                saw_i = True
                if i + 1 < len(tokens) and not tokens[i + 1].startswith('-'):
                    o.notes.append(f"-i {_mask(tokens[i + 1])}: the camera is Nova's own -i; this URL is ignored")
                    i += 1
                i += 1
                continue
            if token.startswith('-') and len(token) > 1 and not token[1].isdigit():
                key = token.lstrip('-')
                nxt = tokens[i + 1] if i + 1 < len(tokens) else None
                # a value may itself start with '-' only if it looks like a number (e.g. -crf -1)
                if nxt is not None and (not nxt.startswith('-') or re.match(r'^-\d', nxt)):
                    parsed.append((key, nxt, 'out' if saw_i else None)); i += 2
                else:
                    parsed.append((key, "1", 'out' if saw_i else None)); i += 1
            else:
                i += 1
        if saw_i:
            parsed = [(k, v, s or 'in') for k, v, s in parsed]

        for key, value, side in parsed:
            FFmpegParser._route(o, key, str(value).strip(), side)
        return o

    @staticmethod
    def _route(o: FFOpts, key: str, v: str, side):
        base, _, spec = key.partition(':')
        spec = spec.lower()

        # ---- handled by Nova itself ------------------------------------------------------
        if base in ('c', 'codec'):
            if spec == 'a': o.a_encoder = v
            else: o.v_encoder = v
            return
        if base == 'vcodec': o.v_encoder = v; return
        if base == 'acodec': o.a_encoder = v; return
        if base == 'vtag' or (base == 'tag' and spec in ('', 'v')):
            o.v_tag = v; return
        if base == 'an': o.no_audio = True; return
        if base == 'f':
            if side == 'in': o.in_fmt = v
            else: o.fmt = v; o.mux['f'] = v
            return
        if base == 'vf' or (base == 'filter' and spec in ('', 'v')):
            o.vf = v; return
        if base == 'metadata':
            k, _, mv = v.partition('=')
            if k: o.metadata[k.strip()] = mv.strip()
            return
        if base == 'hwaccel_device':
            o.hw_device = v; return
        if base in ('s', 'r', 'pix_fmt') and spec != 'a':
            if side == 'in':          # capture devices: what ffmpeg does with -s/-r/-pix_fmt before -i
                o.open[{'s': 'video_size', 'r': 'framerate', 'pix_fmt': 'pixel_format'}[base]] = v
            else:
                o.enc_v[base] = v     # implemented by AVWriter (scaling, frame dropping, pixel format)
            return
        if base in ('ar', 'ac'):
            o.enc_a[base] = v         # implemented by AVWriter (resampling / channel conversion)
            return
        if base == 'ab':
            o.enc_a['b'] = v; return
        if base == 'filter' and spec == 'a':
            base = 'af'

        # ---- the ffmpeg program's own options: no library equivalent ----------------------
        if base in REMOVED:
            o.ignored.append(f"-{key}")
            o.warnings.append(f"-{key}: {REMOVED[base]}; ignored")
            return
        if base in T.CLI_ONLY and not destinations(base, spec):
            hint = CLI_HINTS.get(base)
            o.ignored.append(f"-{key}")
            o.warnings.append(f"-{key} is an option of the ffmpeg program, not of the FFmpeg libraries"
                              + (f": {hint}" if hint else "") + "; ignored")
            return

        # ---- everything else: where FFmpeg defines an option of that name -------------------
        dests = destinations(base, spec)
        if not dests:
            o.enc_v[base] = v
            o.warnings.append(f"-{key}: no decoder, encoder, container or protocol Nova uses has this option "
                              f"({codec_index().source}); passed to the video encoder, which will report it "
                              f"if it does not know it either")
            return
        if side is not None:
            allowed = IN_SIDES if side == 'in' else OUT_SIDES
            if dests & allowed:
                dests &= allowed
            else:
                other = 'out' if side == 'in' else 'in'
                o.notes.append(f"-{key} is an {_SIDE_NAME[other]}-side option ({', '.join(owners(base)[:4])}) "
                               f"but was given {'before' if side == 'in' else 'after'} -i; applied to the "
                               f"{_SIDE_NAME[other]} anyway")
        for d in dests:
            getattr(o, d)[base] = v


if __name__ == "__main__":
    for s in [
        "-f hls -hls_time 2 -hls_list_size 4 -method PUT -vtag hvc1",
        "-rtsp_transport udp -resize 640x360 -b:v 4M -b:a 128k -g 60 -c:v hevc_nvenc -an",
        "-preset p4 -crf -1 -s 1280x720 -threads 2 -flags +low_delay -t 10 -stimeout 5 -bogus 1",
        "-f v4l2 -s 1280x720 -r 30 -threads 1 -i -threads 4 -b:v 2M -fflags +nobuffer",
    ]:
        o = FFmpegParser.parse(s)
        print(s, "\n  ->", o)
        for w in o.warnings: print("  WARN", w)
        for n in o.notes: print("  NOTE", n)
    print("codec tables from:", codec_index().source)
