#!venv/bin/python3 -u

import os
import sys
import signal
import logging
import argparse

from PySide6.QtCore import Qt, QMetaObject, QTimer
from PySide6.QtGui import QGuiApplication, QSurfaceFormat

import logsync   # installs logging.verbose / logging.trace before any module logs
from util import parse_time, mask_url
from ffparser import NovaParser


def log_level_type(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return str(value).upper()


if __name__ == "__main__":
    parser = NovaParser(
        description='Nova: High-performance RTSP Player',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument(
        "-i", "--input",
        dest="url",
        type=str,
        required=True,
        help="Video source: rtsp/rtmp/srt URL, onvif://, xmeye://, hik://, dahua://, "
             "a capture device ('0' = /dev/video0 or avfoundation 0, 'dshow://video=NAME' on Windows)"
    )
    parser.add_argument(
        "-n", "--name", "--camera-name",
        dest="camera_name",
        type=str,
        help="Camera name: the window title becomes 'Nova - NAME', snapshots and recordings go to NAME/."
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
        help='Direct FFmpeg options, e.g. "-rtsp_transport udp -b:v 4M -g 60 -movflags +faststart"'
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

    args = parser.parse_args()

    # Validate the record segmentation interval
    try:
        record_interval = parse_time(args.record_interval)
    except ValueError as e:
        print(f'{str(e)}')
        sys.exit(-1)

    # Validate the log level
    log_level = args.log_level
    if isinstance(log_level, str):
        log_level = log_level.upper()
        log_levels = ["CRITICAL", "ERROR", "WARNING", "INFO", "VERBOSE", "DEBUG", "TRACE"]
        if log_level not in log_levels:
            print(f"Invalid logging level. Please choose from {log_levels} or use a numeric value.")
            sys.exit(-1)
    else:
        if not (0 <= log_level <= 50):
            print(f"Invalid logging level number: {log_level}. Use a value between 0 and 50.")
            sys.exit(-1)


    # Resolve directories
    output_dir = args.output_dir
    snapshot_dir = args.snapshot_dir or output_dir or "./snapshots"
    recording_dir = args.recording_dir or output_dir or "./videos"
    args.accel = None if not args.accel or args.accel.lower() == "none" else args.accel.lower()

    logging.basicConfig(
        stream=sys.stdout,
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y%m%d %H:%M:%S",
        force=True,
    )
    logging.debug(f"log_level={log_level}")
    logsync.setup_av_logging()

    logging.debug(f"Input Arguments: {[mask_url(a) for a in sys.argv[1:]]}")
    logging.debug(f"headless={args.headless}, auto_record={args.auto_record}, auto_audio={args.auto_audio}")
    logging.debug(f"camera_name={args.camera_name}, snapshot_dir={snapshot_dir}, recording_dir={recording_dir}")

    if args.headless:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    elif sys.platform.startswith("linux") and "QT_QPA_PLATFORM" not in os.environ \
            and os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"):
        # A Wayland compositor does not let a window place itself, XWayland does (GNOME and KDE honour
        # X11 positions), and NVIDIA's EGL shows nothing for a frameless GL window on Wayland.
        # QT_QPA_PLATFORM=wayland (e.g. in [env]) still gives a native window.
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        logging.info("Wayland session: running under XWayland (QT_QPA_PLATFORM=wayland for a native window)")

    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)
    fmt.setRenderableType(QSurfaceFormat.OpenGL)
    if args.opengl:
        # NovaGL's shaders need a 3.3 core context (macOS only offers 2.1 legacy otherwise)
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)

    # Qt must not see Nova's options: on X11 it would consume --geometry (as an X geometry string,
    # overriding the window size), -display, -title, ...  Use QT_* environment variables instead.
    app = QGuiApplication([sys.argv[0]])
    app.setOrganizationName("judvi")
    app.setApplicationName("Nova")
    app.setDesktopFileName("nova")      # the app id Wayland compositors see (else the python binary's name)

    if app.platformName() == "wayland":
        # PyOpenGL defaults to GLX; a Wayland window's context is EGL and every GL call would fail
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

    from novacore import NovaCore
    from view import Nova
    from novagl import NovaGL

    logging.debug(f"[Nova] {mask_url(args.url)}")

    core = NovaCore(
        url=args.url,
        fps=args.fps,
        opengl=args.opengl,
        accel=args.accel,
        gpu=args.gpu,
        headless=args.headless,
        auto_record=args.auto_record,
        encoder=args.encoder,
        auto_audio=args.auto_audio,
        camera_name=args.camera_name,
        snapshot_dir=snapshot_dir,
        recording_dir=recording_dir,
        record_interval=record_interval,
        ffmpeg=args.ffmpeg,
        target_url=args.target,
    )

    nova = None
    if not args.headless:
        mode_name = "OpenGL (GPU)" if args.opengl else "Standard (CPU)"
        logging.info(f"Starting visual window in {mode_name} mode.")
        ViewClass = NovaGL if args.opengl else Nova
        nova = ViewClass(
            core,
            grid=args.grid_size,
            border=args.border,
            geometry=args.geometry,
            zoom=args.zoom,
        )
    else:
        # nobody owns a window: a fatal worker error must end the process
        core.exit_sig.connect(lambda: QTimer.singleShot(500, app.quit))

    # Ctrl-C / SIGTERM (nova stop) quit through the event loop; aboutToQuit saves the window
    # geometry/zoom that closeEvent would otherwise have persisted.
    if nova is not None:
        app.aboutToQuit.connect(nova.cleanup)

    # the view is connected before the worker starts so the first size signal is never missed
    core.start()

    t = QTimer(); t.start(200); t.timeout.connect(lambda: None)   # let Python see signals
    def signal_handler(sig, frame):
        a = QGuiApplication.instance()
        if a: QMetaObject.invokeMethod(a, "quit", Qt.QueuedConnection)
    signal.signal(signal.SIGINT, signal_handler)
    for name in ("SIGTERM", "SIGBREAK"):          # SIGBREAK: Windows Ctrl-Break, sent by nova stop
        sig = getattr(signal, name, None)
        if sig is not None:
            try: signal.signal(sig, signal_handler)
            except (ValueError, OSError): pass

    rt = app.exec()
    core.stop()
    logging.debug("Nova exited gracefully.\n")
    sys.exit(rt)
