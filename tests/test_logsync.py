"""The masking log filter and the excepthooks: no password reaches a log line or a traceback."""
import logging
import sys
import threading

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
