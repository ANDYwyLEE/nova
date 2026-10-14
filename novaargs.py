# novaargs.py
"""nova.py's command line, in one place: nova.py parses it, the help overlay (helper.py) and the manual
(docs/gen_keys.py) list the same options with the same words, so the manual cannot drift from --help."""
import argparse

from ffparser import NovaParser


def log_level_type(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return str(value).upper()


def build_parser():
    parser = NovaParser(
        description='Nova: High-performance RTSP Player',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument(
        "-i", "--input",
        dest="url",
        type=str,
        required=True,
        help="Video source: rtsp/rtmp/srt URL; a brand shortcut that finds the RTSP URL (onvif://, hik://, dahua://, "
             "xmeye://, reolink://, axis://, tapo://, unv:// followed by user:pw@host[:port][/CHANNEL][/STREAM]); "
             "a capture device ('0' = /dev/video0 or avfoundation 0, 'dshow://video=NAME' on Windows)"
    )
    parser.add_argument(
        "-n", "--name", "--camera-name",
        dest="camera_name",
        type=str,
        help="Camera name: the window title becomes 'Nova - NAME', recordings go to videos/NAME/ and snapshots are named NAME_<time>.png."
    )
    parser.add_argument(
        "--opengl",
        action="store_true",
        help="Render with OpenGL shaders (zero-copy NV12/YUV upload; recommended)."
    )
    parser.add_argument(
        "--accel",
        type=str,
        default=None,
        help='Hardware decoding backend (cuda, qsv, vaapi, videotoolbox, d3d11va, dxva2, rkmpp, v4l2m2m ...)'
    )
    parser.add_argument(
        "--gpu",
        type=str,
        default=None,
        help="GPU index for hardware decoding/encoding (e.g. 1; a list 0,1 picks one at random). "
             "Default: NOVA_GPU_DEVICES, else 0."
    )
    parser.add_argument(
        "-f", "--fps",
        type=int,
        default=30,
        help="Target display frame rate."
    )
    parser.add_argument(
        "-b", "--border",
        action="store_true",
        help="Enable the colored window border."
    )
    parser.add_argument(
        "-g", "--grid-size",
        type=int,
        default=20,
        help="Grid size for window alignment in pixels."
    )
    parser.add_argument(
        "--geometry",
        type=str,
        help="Initial window placement x,y,w,h; overrides the remembered one (nova start --layout uses it)."
    )
    parser.add_argument(
        "--zoom",
        type=str,
        help="Initial zoom region as ratios rx,ry,rw,rh of the picture (0,0,1,1 = whole picture)."
    )
    parser.add_argument(
        "-a", "--audio", "--enable-audio",
        dest="auto_audio",
        action="store_true",
        help="Enable audio playback automatically on startup."
    )
    parser.add_argument(
        "--headless", "--no-gui",
        dest="headless",
        action="store_true",
        help="Run in headless mode without showing the visual camera window."
    )
    parser.add_argument(
        "-r", "--record", "--auto-record",
        dest="auto_record",
        action="store_true",
        help="Start recording automatically on startup."
    )
    parser.add_argument(
        "-e", "--encoder",
        type=str,
        help="Explicitly specify video encoder (e.g., h264_nvenc, hevc_nvenc, libx264)"
    )
    parser.add_argument(
        "--record-interval",
        type=str,
        default="30m",
        help="Recording segment length (e.g. 10m, 1h, 3600s)."
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        help="Base directory for snapshots and recordings."
    )
    parser.add_argument(
        "--snapshot-dir",
        type=str,
        help="Directory for snapshots (overrides --output-dir for snapshots)."
    )
    parser.add_argument(
        "--recording-dir",
        type=str,
        help="Directory for recorded videos (overrides --output-dir for recordings)."
    )
    parser.add_argument(
        '--ffmpeg',
        type=str,
        default='',
        help='FFmpeg options as on an ffmpeg command line, e.g. "-rtsp_transport udp -b:v 4M -g 60 -movflags +faststart"; '
             'an -i may separate input from output options (docs/ffmpeg-options.md)'
    )
    parser.add_argument(
        "-t", "--target",
        type=str,
        help="Live stream URL (e.g. YouTube, Twitch) or output path"
    )
    parser.add_argument(
        "-l", "--log-level",
        type=log_level_type,  # custom function
        default="INFO",
        help="Logging level (NAME or NUMBER; e.g., DEBUG, INFO, 5, 10)."
    )

    parser.add_argument(
        "--camera", metavar="ID",
        help="Read URL, name and options of camera ID from cameras.ini (what the nova launcher passes)."
    )
    parser.add_argument("--config", metavar="FILE", help="The cameras.ini that --camera reads.")
    parser.add_argument("--control", help=argparse.SUPPRESS)      # control channel address (control.py)
    return parser


def option_table():
    """{'-f, --fps': 'Target display frame rate. Default: 30.', ...} for every public option."""
    rows = {}
    for a in build_parser()._actions:
        if not a.option_strings or a.help is argparse.SUPPRESS or isinstance(a, argparse._HelpAction):
            continue
        text = a.help.rstrip(".") + "."
        if a.default not in (None, False, "", argparse.SUPPRESS) and "default" not in text.lower():
            text += f" Default: {a.default}."
        rows[", ".join(a.option_strings)] = text
    return rows
