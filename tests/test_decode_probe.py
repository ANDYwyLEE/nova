"""The TCP probe before av.open(): bad URLs, several addresses, DNS that hangs, stop()."""
import logging
import socket
import threading
import time

import pytest
from PySide6.QtGui import QGuiApplication

import videoworker
from videoworker import VideoWorker


@pytest.fixture
def worker():
    QGuiApplication.instance() or QGuiApplication([])
    return VideoWorker("rtsp://x", 30, False, None, True, None)


@pytest.fixture
def listening():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(4)
    yield s.getsockname()[1]
    s.close()


@pytest.fixture
def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _infos(*ports):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p)) for p in ports]


@pytest.mark.parametrize("url", [
    "rtsp://admin:se/cret@127.0.0.1:554/stream",      # '/' in the password: "port" becomes 'se'
    "rtsp://admin:se#cret@127.0.0.1/stream",
    "rtsp://admin:pass@127.0.0.1:99999/stream",       # port out of range
    "rtsp://admin:p[1@127.0.0.1/stream",             # '[' makes urlparse raise
])
def test_unparsable_url_does_not_kill_the_worker(worker, url, caplog):
    caplog.set_level(logging.DEBUG)
    assert worker._probe_host(url, 1.0) is None           # no probe; av.open() reports the error
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "cret" not in text and "'se'" not in text and "p[1" not in text
    if "99999" not in url:
        assert "percent-encoded" in text and "percent-encode" in worker._url_hint


def test_any_answering_address_is_enough(worker, monkeypatch, listening, closed_port):
    # dual-stack host whose first address is dead: the old probe gave up at the first refusal
    monkeypatch.setattr(videoworker.socket, "getaddrinfo", lambda *a, **k: _infos(closed_port, listening))
    assert worker._probe_host("rtsp://cam.example/x", 2.0) is None


def test_all_addresses_refusing_is_reported_at_once(worker, monkeypatch, closed_port):
    monkeypatch.setattr(videoworker.socket, "getaddrinfo", lambda *a, **k: _infos(closed_port, closed_port))
    t0 = time.monotonic()
    reason = worker._probe_host("rtsp://cam.example:554/x", 5.0)
    assert reason and "refused" in reason.lower() and reason.startswith("cam.example:554")
    assert time.monotonic() - t0 < 2


def test_dns_failure_and_bad_names(worker, monkeypatch):
    def gai(*a, **k): raise socket.gaierror(-2, "Name or service not known")
    monkeypatch.setattr(videoworker.socket, "getaddrinfo", gai)
    assert worker._probe_host("rtsp://nosuch.example/x", 1.0) == "nosuch.example: Name or service not known"
    def idna(*a, **k): raise UnicodeError("label too long")
    monkeypatch.setattr(videoworker.socket, "getaddrinfo", idna)
    assert "label too long" in worker._probe_host("rtsp://bad.example/x", 1.0)


def test_slow_dns_is_bounded_and_stop_interrupts_it(worker, monkeypatch):
    monkeypatch.setattr(videoworker.socket, "getaddrinfo", lambda *a, **k: time.sleep(3) or _infos(1))
    t0 = time.monotonic()
    assert worker._probe_host("rtsp://slow.example/x", 0.5) == "slow.example: name lookup timed out"
    assert time.monotonic() - t0 < 1.5
    threading.Timer(0.3, lambda: setattr(worker, "run_flag", False)).start()
    t0 = time.monotonic()
    worker._probe_host("rtsp://slow.example/x", 10.0)
    assert time.monotonic() - t0 < 1.0


def test_silent_host_honours_stop(worker, monkeypatch):
    # an address that never answers (TEST-NET-1 is not routed; some CI networks refuse at once instead)
    monkeypatch.setattr(videoworker.socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 554))])
    threading.Timer(0.3, lambda: setattr(worker, "run_flag", False)).start()
    t0 = time.monotonic()
    worker._probe_host("rtsp://192.0.2.1/x", 10.0)
    assert time.monotonic() - t0 < 1.5


def test_open_container_keeps_retrying_on_a_bad_url(worker, caplog, monkeypatch):
    """The whole connection step with a password containing '/': no exception, a hint on screen, and
    the password stays out of the log although PyAV puts the URL into its error text."""
    def fake_open(url, **kw):
        raise videoworker.av.error.InvalidDataError(1094995529, "Invalid data found when processing input", url)
    monkeypatch.setattr(videoworker.av, "open", fake_open)
    worker.clock.sleep = lambda ms: None
    shown = []
    worker.stat_sig.connect(shown.append)
    caplog.set_level(logging.DEBUG)
    assert worker._open_container("rtsp://admin:se/cret@127.0.0.1:1/stream") is None
    assert shown and shown[-1].startswith("Failed:") and "percent-encode" in shown[-1]
    assert not any("cret" in r.getMessage() for r in caplog.records)
