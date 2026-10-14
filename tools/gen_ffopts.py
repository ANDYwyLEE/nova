#!/usr/bin/env python3
"""Generate ffopts_tables.py from a real FFmpeg: the option names libav actually knows.

    python tools/gen_ffopts.py --ffmpeg ~/deps/bin/ffmpeg [--source ~/deps/ffmpeg] > ffopts_tables.py

Nova routes every "-key value" of --ffmpeg to the place where FFmpeg defines an option of that
name: the AVCodecContext class (generic codec options, with their encoding/decoding and
video/audio flags), the private class of a decoder or encoder, the AVFormatContext class, the
private class of a demuxer, muxer or protocol, or the hardware-device constructors.  The ffmpeg
program itself has options that are not AVOptions ("-t", "-map", "-re", ...); they are listed
so that Nova can say so instead of passing them to an encoder.

Everything is read from `ffmpeg -h ...`, which prints the AVOption tables of the build it was
made from, so the tables match the FFmpeg version they were generated from (the PyPI wheel's
bundled FFmpeg and a custom build of the same major version share them).  Components that this
ffmpeg lacks (dshow on Linux, say) are read from the FFmpeg source tree when --source is given.

The components are limited to what Nova can meet: camera demuxers and capture devices, the
decoders and encoders Nova picks between, the containers it writes, input protocols.
"""
import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- components Nova can meet
DEMUXERS = [
    "rtsp", "sdp", "rtp", "hls", "mpegts", "mpegtsraw", "mov", "matroska", "flv", "live_flv",
    "h264", "hevc", "mjpeg", "mpjpeg", "image2",
    # capture devices (libavdevice)
    "v4l2", "dshow", "avfoundation", "gdigrab", "x11grab",
]
MUXERS = ["mp4", "mov", "matroska", "mpegts", "flv", "hls", "segment", "null"]
PROTOCOLS = ["tcp", "udp", "http", "https", "rtmp", "rtmps", "rtp", "srt", "tls", "file"]

_VIDEO = ["h264", "hevc", "mjpeg", "mpeg4", "mpeg2video", "av1", "vp8", "vp9", "vc1"]
DECODERS = _VIDEO + ["libdav1d"] + [
    f"{c}_{suf}" for suf in ("cuvid", "qsv", "v4l2m2m", "rkmpp", "mediacodec", "amf")
    for c in ("h264", "hevc", "mjpeg", "mpeg4", "mpeg2", "av1", "vp8", "vp9", "vc1")
]
ENCODERS = ["libx264", "libx265", "libsvtav1", "libaom-av1", "mjpeg", "mjpeg_qsv", "mjpeg_vaapi"] + [
    f"{c}_{suf}" for suf in ("nvenc", "qsv", "amf", "vaapi", "videotoolbox", "v4l2m2m", "mf", "rkmpp", "mediacodec")
    for c in ("h264", "hevc", "av1")
] + ["aac", "libopus", "libmp3lame", "libfdk_aac", "aac_mf", "aac_at"]

# libavutil/hwcontext_*.c read these from the dictionary given to av_hwdevice_ctx_create()
# (they are not AVOptions, so no ffmpeg help lists them); --source refreshes the list.
HWDEVICE_DEFAULT = {
    "cuda": ["primary_ctx", "current_ctx"],
    "vaapi": ["connection_type", "kernel_driver", "vendor_id", "driver"],
    "qsv": ["child_device", "child_device_type"],
    "d3d11va": ["debug", "vendor_id"],
    "d3d12va": ["debug"],
    "vulkan": ["instance_extensions", "device_extensions", "layers", "debug", "limit_queues",
               "linear_images", "disable_multiplane", "avoid_host_import"],
    "opencl": ["device_type", "platform_name", "device_name"],
}
# Source files of components the ffmpeg at hand may have been built without (Windows/macOS/Android
# backends, external libraries): their AVOption entries are read from the FFmpeg source tree.
SOURCE_DEMUXERS = {
    "dshow": ["libavdevice/dshow.c"], "avfoundation": ["libavdevice/avfoundation.m"],
    "gdigrab": ["libavdevice/gdigrab.c"], "v4l2": ["libavdevice/v4l2.c"], "x11grab": ["libavdevice/xcbgrab.c"],
}
_AMF = ["libavcodec/amfenc.c", "libavcodec/amfenc.h"]
_QSV = ["libavcodec/qsvenc.h", "libavcodec/qsvenc.c"]
SOURCE_CODECS = {        # name -> (kind, flags, files)
    "h264_amf": ("encoder", "EV", ["libavcodec/amfenc_h264.c"] + _AMF),
    "hevc_amf": ("encoder", "EV", ["libavcodec/amfenc_hevc.c"] + _AMF),
    "av1_amf": ("encoder", "EV", ["libavcodec/amfenc_av1.c"] + _AMF),
    "h264_amf_dec": ("decoder", "DV", ["libavcodec/amfdec.c"]),
    "h264_qsv": ("encoder", "EV", ["libavcodec/qsvenc_h264.c"] + _QSV),
    "hevc_qsv": ("encoder", "EV", ["libavcodec/qsvenc_hevc.c"] + _QSV),
    "av1_qsv": ("encoder", "EV", ["libavcodec/qsvenc_av1.c"] + _QSV),
    "mjpeg_qsv": ("encoder", "EV", ["libavcodec/qsvenc_jpeg.c"] + _QSV),
    "h264_qsv_dec": ("decoder", "DV", ["libavcodec/qsvdec.c"]),
    "h264_videotoolbox": ("encoder", "EV", ["libavcodec/videotoolboxenc.c"]),
    "h264_mf": ("encoder", "EV", ["libavcodec/mfenc.c"]),
    "aac_mf": ("encoder", "EA", ["libavcodec/mfenc.c"]),
    "h264_mediacodec": ("encoder", "EV", ["libavcodec/mediacodecenc.c"]),
    "h264_mediacodec_dec": ("decoder", "DV", ["libavcodec/mediacodecdec.c"]),
    "h264_rkmpp": ("encoder", "EV", ["libavcodec/rkmppenc.c"]),
    "h264_rkmpp_dec": ("decoder", "DV", ["libavcodec/rkmppdec.c"]),
    "h264_v4l2m2m": ("encoder", "EV", ["libavcodec/v4l2_m2m_enc.c", "libavcodec/v4l2_m2m.h"]),
    "h264_v4l2m2m_dec": ("decoder", "DV", ["libavcodec/v4l2_m2m_dec.c", "libavcodec/v4l2_m2m.h"]),
    "h264_vaapi": ("encoder", "EV", ["libavcodec/vaapi_encode_h264.c", "libavcodec/vaapi_encode.h", "libavcodec/hw_base_encode.h"]),
    "hevc_vaapi": ("encoder", "EV", ["libavcodec/vaapi_encode_h265.c"]),
    "av1_vaapi": ("encoder", "EV", ["libavcodec/vaapi_encode_av1.c"]),
    "libsvtav1": ("encoder", "EV", ["libavcodec/libsvtav1.c"]),
    "libaom-av1": ("encoder", "EV", ["libavcodec/libaomenc.c"]),
    "libdav1d": ("decoder", "DV", ["libavcodec/libdav1d.c"]),
    "libopus": ("encoder", "EA", ["libavcodec/libopusenc.c"]),
    "libmp3lame": ("encoder", "EA", ["libavcodec/libmp3lame.c"]),
    "libfdk_aac": ("encoder", "EA", ["libavcodec/libfdk-aacenc.c"]),
    "aac_at": ("encoder", "EA", ["libavcodec/audiotoolboxenc.c"]),
}

_OPT = re.compile(r"^  -(\S+)\s+\[?<([^>]*)>\s*\]?\s*([EDFVASXRBTP.]{10,12})\s*(.*)$")   # [<int>   ] = array
_TITLE = re.compile(r"^(.*) AVOptions:$")


def run(ffmpeg, *args):
    p = subprocess.run([ffmpeg, "-hide_banner", *args], capture_output=True, text=True)
    return p.stdout + p.stderr


def parse_sections(text):
    """{title: {name: flags}} for every 'X AVOptions:' block; flags is a subset of 'EDVA'."""
    out, title = {}, None
    for line in text.splitlines():
        m = _TITLE.match(line)
        if m:
            title = m.group(1)
            out.setdefault(title, {})
            continue
        if title is None:
            continue
        if not line.strip():
            title = None
            continue
        m = _OPT.match(line)
        if not m:
            continue                                   # a named constant of the option above
        name, fl = m.group(1), m.group(3)
        # deprecated options (flag P) stay: they still work, and PyAV lists them too
        flags = "".join(c for c, want in zip(fl, "EDFVA") if c == want and c in "EDVA")
        out[title][name] = flags
    return out


def component(ffmpeg, kind, name):
    """{name: flags} of one component's private class, or None if this ffmpeg lacks it."""
    text = run(ffmpeg, "-h", f"{kind}={name}")
    # "Unknown format 'dshow'.", "Unknown protocol 'srt'.", "Codec 'h264_amf' is not recognized by FFmpeg."
    if re.search(r"^(Unknown|No such) \w+ '|is not recognized", text, re.M | re.I):
        return None
    opts = {}
    for title, table in parse_sections(text).items():
        opts.update(table)
    return opts


def cli_options(full_text):
    """The ffmpeg program's own options: everything listed before the first AVOptions block."""
    names = set()
    for line in full_text.splitlines():
        if _TITLE.match(line):
            break
        m = re.match(r"^-([A-Za-z0-9_]+)", line)
        if m:
            names.add(m.group(1))
    return names


_ENTRY = re.compile(r'\{\s*"([A-Za-z0-9_\-]+)"\s*,\s*(?:"(?:[^"\\]|\\.)*"|NULL|[A-Z_]+\([^)]*\))\s*,'
                    r'\s*[^{};]*?AV_OPT_TYPE_(\w+)')          # the offset may be offsetof(a, b)


def source_table(root, files):
    """Option names in the AVOption entries of C files (macros included): [(name, is_const)]; None
    when no file exists."""
    names, found = [], False
    for f in files:
        try:
            text = Path(root, f).read_text(errors="replace")
        except OSError:
            continue
        found = True
        for m in _ENTRY.finditer(text):
            if m.group(2) != "CONST":
                names.append(m.group(1))
    return names if found else None


def source_hwdevice(src):
    out = {}
    for f in sorted(Path(src, "libavutil").glob("hwcontext_*.c")):
        names = re.findall(r'av_dict_get\(\s*opts\s*,\s*"([A-Za-z0-9_]+)"', f.read_text(errors="replace"))
        if names:
            out[f.stem.replace("hwcontext_", "")] = sorted(set(names))
    return out


def merge(dst, name, flags, owner):
    fl, owners = dst.get(name, ("", ()))
    dst[name] = ("".join(c for c in "EDVA" if c in fl or c in flags), tuple(sorted(set(owners) | {owner})))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ffmpeg", action="append", default=[],
                    help="ffmpeg executable to read the tables from; may be repeated, the first one "
                         "gives the version and the generic tables, the others add components it lacks")
    ap.add_argument("--source", help="FFmpeg source tree, for components none of the ffmpegs was built with")
    a = ap.parse_args()
    ffmpegs = a.ffmpeg or ["ffmpeg"]

    vtext = run(ffmpegs[0], "-version")
    version = vtext.splitlines()[0].split()[2] if vtext else "?"
    m = re.search(r"libavcodec\s+(\d+)\.\s*(\d+)\.\s*(\d+)", vtext)
    lavc = tuple(int(x) for x in m.groups()) if m else ()
    full = run(ffmpegs[0], "-h", "full")
    generic = parse_sections(full)
    codec_generic = generic.get("AVCodecContext", {})
    format_generic = generic.get("AVFormatContext", {})
    url_generic = generic.get("URLContext", {})
    if not codec_generic or not format_generic:
        sys.exit(f"{ffmpegs[0]}: could not read the AVCodecContext/AVFormatContext tables")

    def table(kind, name):
        for ff in ffmpegs:
            t = component(ff, kind, name)
            if t is not None:
                return t
        return None

    dec, enc, demux, mux, proto = {}, {}, {}, {}, {}
    missing, from_source = [], []
    for kind, names, dst in (("decoder", DECODERS, dec), ("encoder", ENCODERS, enc),
                             ("demuxer", DEMUXERS, demux), ("muxer", MUXERS, mux), ("protocol", PROTOCOLS, proto)):
        for name in names:
            t = table(kind, name)
            if t is None:
                missing.append(f"{kind} {name}")
                continue
            for k, fl in t.items():
                merge(dst, k, fl, name)
    if a.source:
        # The source tree is the complete list for its version: its names are merged on top of what
        # the ffmpegs gave (a second, older ffmpeg may lack an option added since), and they stand
        # in for components none of the ffmpegs was built with.
        for name, files in SOURCE_DEMUXERS.items():
            names = source_table(a.source, files)
            if names:
                for k in names: merge(demux, k, "D", name)
                if f"demuxer {name}" in missing:
                    missing.remove(f"demuxer {name}"); from_source.append(f"demuxer {name}")
        for name, (kind, fl, files) in SOURCE_CODECS.items():
            base = name.removesuffix("_dec")
            names = source_table(a.source, files)
            if names:
                for k in names: merge(dec if kind == "decoder" else enc, k, fl, base)
                if f"{kind} {base}" in missing:
                    missing.remove(f"{kind} {base}"); from_source.append(f"{kind} {base}")
    for k, fl in url_generic.items(): merge(proto, k, fl, "URLContext")
    hw = source_hwdevice(a.source) if a.source else HWDEVICE_DEFAULT

    known = set(codec_generic) | set(format_generic) | set(dec) | set(enc) | set(demux) | set(mux) | set(proto)
    cli = sorted(cli_options(full) - known)

    def dump(name, table, doc):
        print(f"\n# {doc}")
        print(f"{name} = {{")
        for k in sorted(table):
            v = table[k]
            print(f"    {k!r}: {v!r},")
        print("}")

    print(f'"""FFmpeg option tables for ffparser.py: generated by tools/gen_ffopts.py, do not edit.')
    print(f"\nFFmpeg {version}, {datetime.date.today()}.  Regenerate after an FFmpeg upgrade:")
    print(f"    python tools/gen_ffopts.py --ffmpeg PATH/TO/ffmpeg [--source FFMPEG_SOURCE_DIR] > ffopts_tables.py")
    print(f'\nFlags: E encoding, D decoding, V video, A audio (the AV_OPT_FLAG_* of the option).  Private')
    print(f'tables map a name to (flags, components that define it).')
    if from_source:
        print(f"\nRead from the source tree (not in the ffmpeg used): {', '.join(from_source)}")
    if missing:
        print(f"\nNot available anywhere ({len(missing)}): {', '.join(missing)}")
    print('"""')
    print(f"\nFFMPEG_VERSION = {version!r}")
    print(f"LIBAVCODEC = {lavc!r}          # av.library_versions['libavcodec'] of the same build")
    dump("CODEC_GENERIC", codec_generic, "AVCodecContext options (every decoder and encoder): name -> flags")
    dump("DECODER_PRIVATE", dec, "private options of the decoders Nova chooses between")
    dump("ENCODER_PRIVATE", enc, "private options of the encoders Nova chooses between")
    dump("FORMAT_GENERIC", format_generic, "AVFormatContext options (every demuxer and muxer): D input, E output")
    dump("DEMUXER_PRIVATE", demux, "private options of camera demuxers and capture devices")
    dump("MUXER_PRIVATE", mux, "private options of the containers Nova writes")
    dump("PROTOCOL", proto, "input protocol options (an output's protocol gets none: PyAV opens it without options)")
    print("\n# options read by av_hwdevice_ctx_create() (libavutil/hwcontext_*.c): device type -> names")
    print("HWDEVICE = {")
    for k in sorted(hw):
        print(f"    {k!r}: {tuple(hw[k])!r},")
    print("}")
    print("\n# options of the ffmpeg program itself: no AVOption of that name exists")
    print("CLI_ONLY = {")
    for i in range(0, len(cli), 8):
        print("    " + ", ".join(repr(x) for x in cli[i:i + 8]) + ",")
    print("}")


if __name__ == "__main__":
    main()
