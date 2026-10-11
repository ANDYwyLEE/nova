#!venv/bin/python3 -u


from PySide6.QtCore import Qt, QMetaObject
from PySide6.QtGui import QGuiApplication, QSurfaceFormat

from PySide6.QtCore import QTimer
import os
import sys
import signal
import logging
import argparse

from util import parse_time
from novacore import NovaCore
from view import Nova
from novagl import NovaGL
from ffparser import NovaParser



def log_level_type(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        val_str = str(value).upper()
        if val_str in ["CRITICAL", "ERROR", "WARNING", "INFO", "VERBOSE", "DEBUG",  "TRACE"]:
            return val_str
        return val_str


if __name__ == "__main__":
    #parser = argparse.ArgumentParser(description="Nova video streaming client")
    parser = NovaParser(
        description='Nova: High-performance RTSP Player',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument(
        "-i", "--input",
        dest="url",
        type=str,
        required=True,
        help="Video source (e.g., rtsp, rtmp, srt, onvif, xmeye, '0' for USB cam)"
    )
    parser.add_argument(
        "-n,", "--name", "--camera-name",
        dest="camera_name",
        type=str,
        help="Logical camera name for the window title and output folders."
    )
    parser.add_argument(
        "--opengl",
        action="store_true",
        help="Enable hardware acceleration using OpenGL (recommended for high performance)."
    )
    parser.add_argument(
        "--accel",
        type=str,
        default=None,
        #choices=['cuda', 'qsv', 'vaapi', 'videotoolbox', 'd3d11va', 'dxva2', 'd3d12va', 'vulkan', "vdpau", "amf", "rkmpp", "omx", "mmal", "v4l2m2m", "nvmpi"],
        help='Hardware acceleration backend. (e.g., cuda for NVIDIA, qsv for Intel)'
    )
    parser.add_argument(
        "-f", "--fps",
        type=int,
        default=30,
        help="Target video frame rate (default: 30)."
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
        help="Grid size for window alignment in pixels (default: 20)."
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
        help="Explicitly specify video encoder (e.g., h264_vaapi, hevc_nvenc)"
    )
    parser.add_argument(
        "--record-interval",
        type=str,
        default="30m",
        help="Recording segment length (e.g. 10m, 1h, 3600s; default: 30m)."
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
        help='Direct FFmpeg options (e.g., "-rtsp_transport udp -resize 640x360")'
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
        help="Logging level (NAME or NUMBER; e.g., DEBUG, INFO, 5, 10; default: INFO)."
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
    args.accel = None if not args.accel or args.accel.lower() == "none" else args.accel

    # Set the logging level
    VERBOSE_LEVEL = 15; TRACE_LEVEL = 5
    def verbose(self, message, *args, **kws):
        if self.isEnabledFor(VERBOSE_LEVEL): self._log(VERBOSE_LEVEL, message, args, **kws)
    def trace(self, message, *args, **kws):
        if self.isEnabledFor(TRACE_LEVEL): self._log(TRACE_LEVEL, message, args, **kws)
    logging.Logger.verbose = verbose
    logging.verbose = lambda msg, *args, **kwargs: logging.getLogger().verbose(msg, *args, **kwargs)
    logging.Logger.trace = trace
    logging.trace = lambda msg, *args, **kwargs: logging.getLogger().trace(msg, *args, **kwargs)
    logging.addLevelName(logging.WARNING, "WARN ")
    logging.addLevelName(logging.INFO, "INFO ")
    logging.addLevelName(TRACE_LEVEL, "TRACE")
    logging.addLevelName(VERBOSE_LEVEL, "VERBOSE")
    logging.addLevelName(VERBOSE_LEVEL, "VERBO")
    logging.basicConfig(
        #filename="nova.log",
        #filemode="a",
        stream=sys.stdout,
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y%m%d %H:%M:%S",
        force=True,
    )
    logging.debug(f"log_level={log_level}")

    from logsync import setup_av_logging
    setup_av_logging()

    logging.debug(f"Input Arguments: {sys.argv[1:]}")
    logging.debug(f"headless={args.headless}, auto_record={args.auto_record}, auto_audio={args.auto_audio}")
    logging.debug(f"camera_name={args.camera_name}, snapshot_dir={snapshot_dir}, recording_dir={recording_dir}")

    if args.headless:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)
    fmt.setRenderableType(QSurfaceFormat.OpenGL)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QGuiApplication(sys.argv)
    app.setOrganizationName("judvi")
    app.setApplicationName("Nova")

    from auth import resolve_input_uri
    url = resolve_input_uri(args.url)
    if url is None:
        logging.critical("Failed to resolve URI. Exiting Nova.")
        sys.exit(1)

    logging.debug(f"[Nova] {url}")

    core = NovaCore(
        url=url,
        fps=args.fps,
        opengl=args.opengl,
        accel=args.accel,
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
    core.start()
    if not args.headless:
        mode_name = "OpenGL (GPU)" if args.opengl else "Standard (CPU)"
        logging.info(f"Starting visual window in {mode_name} mode.")
        ViewClass = NovaGL if args.opengl else Nova
        nova = ViewClass(
            core,
            grid=args.grid_size,
            border=args.border,
        )

    t=QTimer();t.start(200);t.timeout.connect(lambda: None)
    def signal_handler(sig, frame):
        app = QGuiApplication.instance()
        if app:QMetaObject.invokeMethod(app, "quit", Qt.QueuedConnection)
    signal.signal(signal.SIGINT, signal_handler)
    #signal.signal( signal.SIGINT, lambda sig, frame: \
    #    QMetaObject.invokeMethod(app, "quit", Qt.QueuedConnection))

    rt = app.exec()
    core.stop()
    logging.debug("Nova exited gracefully.\n")
    sys.exit(rt)
