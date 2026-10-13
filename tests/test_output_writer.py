"""AVWriter / EncoderManager behaviour that needs no camera: environment parsing, the probe lock."""
import logging
import os
import stat
import tempfile

import pytest

import avwriter


@pytest.fixture
def fresh_manager(monkeypatch):
    """A new EncoderManager singleton for this test only."""
    monkeypatch.setattr(avwriter.EncoderManager, "_instance", None)
    yield avwriter.EncoderManager()


def test_bad_nova_timeout_falls_back_to_the_default_with_one_warning(monkeypatch, caplog):
    monkeypatch.setattr(avwriter, "_bad_timeout_warned", False)
    for good, us in (("5000000", 5_000_000), ("2.5e6", 2_500_000), ("", avwriter.NET_TIMEOUT_US)):
        monkeypatch.setenv("NOVA_TIMEOUT", good)
        assert avwriter._net_timeout_us() == us
    caplog.set_level(logging.WARNING)
    for bad in ("10s", "-1", "inf", "nan"):
        monkeypatch.setenv("NOVA_TIMEOUT", bad)
        assert avwriter._net_timeout_us() == avwriter.NET_TIMEOUT_US
        assert avwriter._net_timeouts("rtmp://host/live/key") == (30.0, 10.0)
    assert len([r for r in caplog.records if "NOVA_TIMEOUT" in r.getMessage()]) == 1


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership checks")
def test_probe_lock_lives_in_a_private_place(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    os.mkdir(tmp_path / "run", 0o700)
    assert avwriter._encoder_lock_path() == str(tmp_path / "run" / "nova_encoder_init.lock")

    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    path = avwriter._encoder_lock_path()
    d = os.path.dirname(path)
    assert d == str(tmp_path / f"nova-{os.getuid()}")
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    os.chmod(d, 0o777)                         # e.g. made by someone else for everyone: not used
    assert avwriter._encoder_lock_path() is None


def test_an_unusable_probe_lock_does_not_cost_the_hardware_encoder(monkeypatch, caplog, fresh_manager):
    attempts = []

    class BusyLock:
        def __init__(self, path, timeout):
            attempts.append(path)

        def acquire(self):
            raise avwriter.Timeout(attempts[-1])

    monkeypatch.setattr(avwriter, "FileLock", BusyLock)
    monkeypatch.setattr(avwriter, "_encoder_lock_path", lambda: "/somewhere/nova_encoder_init.lock")
    monkeypatch.setattr(avwriter.EncoderManager, "_perform_hw_test", lambda self, *a: "h264_nvenc")
    caplog.set_level(logging.WARNING)
    assert fresh_manager.get_encoder("cuda", None, "h264", {}, ("h264",)) == "h264_nvenc"
    assert fresh_manager.get_encoder("cuda", None, "hevc", {}, ("hevc",)) == "h264_nvenc"
    assert len(attempts) == 1                                  # given up on after the first timeout
    assert len([r for r in caplog.records if "probe lock" in r.getMessage()]) == 1
