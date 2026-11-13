#!venv/bin/python3 -u
"""One camera: options (argv, or cameras.ini through --camera / --config), logging, the Qt application
and platform choice, NovaCore and the window, orderly shutdown."""
import os
import sys
import signal
import logging

from util import cpu_message      # util imports nothing compiled
if cpu_message():                 # PySide6 or numpy built for a newer CPU: importing them would end this
    sys.exit("nova: " + cpu_message())

from PySide6.QtCore import Qt, QMetaObject, QTimer, QSize
from PySide6.QtGui import QGuiApplication, QSurfaceFormat, QIcon

import logsync   # installs logging.verbose / logging.trace before any module logs
from util import parse_time, mask_url, NATIVE_APP_ID, window_platform, no_display, NO_DISPLAY, utf8_stdio, MAC, driver_own_libgl
from novaargs import build_parser

ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)     # icons/nova-N.png, rendered by tools/gen_icon.py
WINDOWS_APP_ID = "ANDYwyLEE.Nova"


def app_icon():
    """Nova's icon for the task bar, the window switcher and the Dock. Each size is a file of its own, as
    the small ones are simpler drawings. The Dock shows the picture as it is, so macOS gets the one
    with the margin its own icons have."""
    icon = QIcon()
    if MAC:
        icon.addFile(os.path.join(ICONS, "nova-mac.png"))
    else:
        for size in ICON_SIZES:
            icon.addFile(os.path.join(ICONS, f"nova-{size}.png"), QSize(size, size))
    return icon


def own_taskbar_button():
    """Windows shows python.exe's icon on the task bar for every window of a process that has not named
    itself: with an application id of its own the button gets the window's icon, and Nova's windows
    are grouped apart from other Python programs."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except Exception as e:
        logging.debug(f"[Nova] no application id for the task bar: {e}")


def expand_camera(argv):
    """`--camera ID [--config FILE]`, as the launcher starts a camera: its URL, name and options are
    read from cameras.ini here, so they never appear on the command line, which every local user can
    read (ps). Returns (camera id or None, the arguments to parse)."""
    if "--camera" not in argv:
        return None, list(argv)
    import cams
    argv = list(argv)

    def take(flag):
        if flag not in argv:
            return None
        i = argv.index(flag)
        if i + 1 >= len(argv):
            sys.exit(f"{flag} needs a value")
        value = argv[i + 1]
        del argv[i:i + 2]
        return value

    cid, config = take("--camera"), take("--config") or cams.INI
    defaults, _, cameras = cams.load(config, quiet=True)
    cam = next((c for c in cameras if c.id == cid), None)
    if cam is None:
        sys.exit(f"--camera {cid}: no such camera in {config}")
    return cid, cams.camera_args(cam, defaults) + argv


if __name__ == "__main__":
    utf8_stdio()             # logs/ID.log is this stdout: UTF-8 on Windows too, emoji and all
    if os.name != "nt":
        import cams
        os.umask(cams.umask())   # recordings, snapshots, logs, window settings: this user's only (NOVA_UMASK)
    camera_id, argv = expand_camera(sys.argv[1:])
    parser = build_parser()

    args = parser.parse_args(argv)

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
        # logsync names these levels "VERBO" and "TRACE" in the log lines; logging only knows numbers for them
        log_level = {"VERBOSE": logsync.VERBOSE_LEVEL, "TRACE": logsync.TRACE_LEVEL}.get(log_level, log_level)
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
    logsync.install_excepthooks()

    logging.debug(f"Input Arguments: {[mask_url(a) for a in argv]}" + (f" (camera {camera_id})" if camera_id else ""))
    logging.debug(f"headless={args.headless}, auto_record={args.auto_record}, auto_audio={args.auto_audio}")
    logging.debug(f"camera_name={args.camera_name}, snapshot_dir={snapshot_dir}, recording_dir={recording_dir}")

    if args.headless:
        # no window: never depend on a display or compositor ([env] or the session may name one), or
        # `nova record` dies with the desktop session and cannot start from ssh or cron
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    else:
        if no_display(os.environ):
            logging.critical("❌ " + NO_DISPLAY[0].upper() + NO_DISPLAY[1:])
            sys.exit(-1)
        platform, note = window_platform(os.environ)     # XWayland on a Wayland session, whatever the desktop exports
        if platform:
            os.environ["QT_QPA_PLATFORM"] = platform
            logging.info(note)

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
    own_taskbar_button()
    app = QGuiApplication([sys.argv[0]])
    app.setOrganizationName("nova")        # settings: ~/.config/nova/nova.conf (cams.window_settings)
    app.setApplicationName("nova")
    app.setWindowIcon(app_icon())
    app.setDesktopFileName("nova")      # the app id Wayland compositors see (else the python binary's name)

    # PyOpenGL must use the context API of the window Qt made, or every GL call fails ("no valid
    # context"). It guesses from XDG_SESSION_TYPE, so in a Wayland login it picks EGL even for an
    # XWayland window, whose context is GLX unless QT_XCB_GL_INTEGRATION=xcb_egl.
    if app.platformName().startswith("wayland"):      # "wayland" or "wayland-egl"
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        app.setDesktopFileName(NATIVE_APP_ID)         # KWin rules from `nova layout export` match it
    elif app.platformName() == "xcb":
        egl = os.environ.get("QT_XCB_GL_INTEGRATION", "") == "xcb_egl"
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl" if egl else "glx")
        if os.environ["PYOPENGL_PLATFORM"] == "glx" and driver_own_libgl():
            logging.debug("[Nova] libGL.so.1 is the GPU driver's own (no libglvnd): PyOpenGL uses it for GLX as well")

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
            tile=args.tile,
        )
    else:
        # nobody owns a window: a fatal worker error must end the process
        core.exit_sig.connect(lambda: QTimer.singleShot(500, app.quit))

    # Ctrl-C / SIGTERM (nova stop) quit through the event loop; aboutToQuit saves the window
    # geometry/zoom that closeEvent would otherwise have persisted.
    if nova is not None:
        app.aboutToQuit.connect(nova.cleanup)

    control = None
    if args.control:
        from control import ControlServer
        control = ControlServer(args.control, camera_id or core.camera_name, core, nova, app.quit)
        if not control.listen():
            control = None

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
    if rt == 0 and getattr(nova, "gl_init_failed", False):
        rt = 1                                    # the window never worked: a launcher must see a failure
    if control is not None:
        control.close()
    if not core.stop() or core.vid_worker.isRunning():
        # stuck in a blocking open/resolve: destroying a running QThread aborts the process (SIGABRT,
        # a crash report); recordings and streams are closed already, so leave without it
        logging.warning("[Nova] decoder thread still busy at exit; leaving without waiting for it")
        logging.shutdown()
        os._exit(rt)
    logging.debug("Nova exited gracefully.\n")
    sys.exit(rt)
