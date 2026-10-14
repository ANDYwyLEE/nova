"""The masking log filter and the excepthooks: no password reaches a log line or a traceback; FFmpeg's
log threshold follows -l and known noise is demoted or dropped."""
import logging
import sys
import threading

import av.logging
import pytest

import logsync


def capture(fn):
    records = []
    h = logging.Handler()
    h.emit = lambda r: records.append(h.format(r))
    h.setFormatter(logging.Formatter("%(message)s"))
    h.addFilter(logsync.CredentialMaskFilter())
    log = logging.getLogger("test_logsync")
    log.addHandler(h); log.propagate = False
    try:
        fn(log)
    finally:
        log.removeHandler(h)
    return "\n".join(records)


def test_message_args_and_traceback_are_masked():
    def go(log):
        log.error("open %s", "rtsp://admin:S3cret@10.0.0.1/x")
        try:
            raise OSError("failed: 'rtsp://u:pw2@h/y'")
        except OSError:
            log.exception("boom")
    out = capture(go)
    assert "S3cret" not in out and "pw2" not in out
    assert "rtsp://admin:***@10.0.0.1/x" in out and "rtsp://u:***@h/y" in out


def test_uncaught_exceptions_go_through_logging(monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    h = logging.Handler(); h.emit = lambda r: seen.append(r)
    h.addFilter(logsync.CredentialMaskFilter())
    root = logging.getLogger(); root.addHandler(h)
    try:
        logsync.install_excepthooks()
        t = threading.Thread(target=lambda: (_ for _ in ()).throw(OSError("rtsp://a:pw3@h/z")))
        t.start(); t.join()
    finally:
        root.removeHandler(h)
    assert seen and "pw3" not in seen[0].exc_text and "rtsp://a:***@h/z" in seen[0].exc_text


# ---------------------------------------------------------------- FFmpeg's log through PyAV
@pytest.mark.parametrize("level, threshold", [
    (60, av.logging.FATAL), (logging.CRITICAL, av.logging.FATAL), (logging.ERROR, av.logging.ERROR),
    (35, av.logging.ERROR), (logging.WARNING, av.logging.WARNING), (25, av.logging.WARNING),
    (logging.INFO, av.logging.INFO), (logsync.VERBOSE_LEVEL, av.logging.INFO), (12, av.logging.INFO),
    (logging.DEBUG, av.logging.VERBOSE), (logsync.TRACE_LEVEL, av.logging.DEBUG), (1, av.logging.DEBUG),
])
def test_ffmpeg_threshold_follows_the_nova_level(level, threshold):
    assert logsync.av_threshold(level) == threshold


@pytest.fixture
def root_handler(monkeypatch):
    """A root handler of our own, the root level restored afterwards; records are returned formatted."""
    records = []
    h = logging.Handler()
    h.emit = lambda r: records.append(f"{r.levelno} {r.name} {r.getMessage()}")
    root = logging.getLogger()
    old_level, old_av, old_libav = root.level, av.logging.get_level(), logging.getLogger("libav").level
    root.addHandler(h)
    monkeypatch.delenv("NOVA_AV_LOG", raising=False)
    try:
        yield records, h
    finally:
        root.removeHandler(h)
        root.setLevel(old_level)
        av.logging.set_level(old_av)
        logging.getLogger("libav").setLevel(old_libav)


def test_setup_sets_threshold_logger_and_filters(root_handler, monkeypatch):
    records, h = root_handler
    logging.getLogger().setLevel(logging.INFO)
    logsync.setup_av_logging()
    assert av.logging.get_level() == av.logging.INFO and logging.getLogger("libav").level == logging.INFO
    assert [type(f).__name__ for f in h.filters] == ["AVNoiseFilter", "CredentialMaskFilter"]
    logsync.setup_av_logging()                                   # idempotent: filters are not stacked
    assert [type(f).__name__ for f in h.filters] == ["AVNoiseFilter", "CredentialMaskFilter"]
    monkeypatch.setenv("NOVA_AV_LOG", "trace")                   # raw: FFmpeg's threshold, no noise rules
    logsync.setup_av_logging()
    assert av.logging.get_level() == av.logging.TRACE and logging.getLogger("libav").level == 1
    assert [type(f).__name__ for f in h.filters] == ["CredentialMaskFilter"]
    monkeypatch.setenv("NOVA_AV_LOG", "loud")
    logsync.setup_av_logging()
    assert av.logging.get_level() == av.logging.INFO and any("NOVA_AV_LOG='loud'" in r for r in records)


def test_noise_is_demoted_or_dropped(root_handler):
    records, h = root_handler
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logsync.setup_av_logging()
    h264 = logging.getLogger("libav.h264")
    h264.error("non-existing PPS 0 referenced")                  # joining between keyframes: DEBUG, hidden at INFO
    h264.error("Could not find ref with POC 12")
    h264.warning("SEI type 5 overread by 8 bits")
    logging.getLogger("libav.flv").warning("Failed to update header with correct duration.")
    h264.error("a real error")
    logging.getLogger("other").error("non-existing PPS 0 referenced")   # not FFmpeg's: untouched
    assert records == ["40 libav.h264 a real error", "40 other non-existing PPS 0 referenced"]
    root.setLevel(logsync.TRACE_LEVEL)
    logsync.setup_av_logging()
    records.clear()
    h264.error("non-existing PPS 0 referenced")
    h264.log(5, "nal_unit_type: 5(IDR), nal_ref_idc: 3")        # one per NAL unit: dropped even at TRACE
    h264.log(5, "Reinit context to 1920x1088, pix_fmt: yuv420p")
    assert records == ["10 libav.h264 non-existing PPS 0 referenced",
                       "5 libav.h264 Reinit context to 1920x1088, pix_fmt: yuv420p"]


def test_av_quiet_restores_the_threshold(root_handler):
    av.logging.set_level(av.logging.INFO)
    with logsync.av_quiet():
        assert av.logging.get_level() == av.logging.FATAL
    assert av.logging.get_level() == av.logging.INFO
    av.logging.set_level(None)
    with logsync.av_quiet():
        pass
    assert av.logging.get_level() is None
