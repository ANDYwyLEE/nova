"""The per-window settings files: one file per camera window, read of the former shared file, and no
waiting between windows that close at the same moment."""
import os
import subprocess
import sys
import time

import pytest

from PySide6.QtCore import QSettings

from util import WindowStore, settings_dir


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_SETTINGS_DIR", str(tmp_path))
    return WindowStore()


def test_write_read_remove(store, tmp_path):
    assert store.dir == str(tmp_path / "windows")
    assert store.read("abc") == (None, None)
    assert store.write("abc", "1,2,300,200", "0,0,1,1")
    assert store.read("abc") == ("1,2,300,200", "0,0,1,1")
    assert os.path.exists(store.path("abc")) and store.hashes() == ["abc"]
    store.write("abc", "5,6,300,200", None)                     # a None removes that value
    assert store.read("abc") == ("5,6,300,200", None)
    store.remove("abc")
    assert store.read("abc") == (None, None) and store.hashes() == []


def test_former_shared_file_is_read_until_the_window_has_its_own(store):
    legacy = QSettings("nova", "nova")                           # conftest points QSettings at a temp dir
    legacy.setValue("geom_old1", "10,20,320,180"); legacy.setValue("zoom_old1", "0,0,0.5,0.5"); legacy.sync()
    assert store.read("old1") == ("10,20,320,180", "0,0,0.5,0.5")
    store.write("old1", "11,21,320,180", "0,0,1,1")
    assert store.read("old1") == ("11,21,320,180", "0,0,1,1")
    legacy.remove("geom_old1"); legacy.remove("zoom_old1"); legacy.sync()


def test_settings_dir_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVA_SETTINGS_DIR", str(tmp_path / "here"))
    assert settings_dir() == str(tmp_path / "here")
    monkeypatch.delenv("NOVA_SETTINGS_DIR")
    assert settings_dir() and os.path.basename(settings_dir()) == "nova"


WRITER = """
import sys, time
from util import WindowStore
t0 = time.monotonic()
WindowStore().write(sys.argv[1], "1,2,3,4", "0,0,1,1")
print(f"{time.monotonic() - t0:.3f}")
"""


def test_thirteen_windows_closing_at_once_do_not_wait_for_each_other(tmp_path):
    """Each window has its own file: thirteen simultaneous writers all finish at once (the shared
    QSettings file made the last of them wait seconds, the lock being retried with growing sleeps)."""
    env = dict(os.environ, NOVA_SETTINGS_DIR=str(tmp_path), PYTHONPATH=os.path.dirname(os.path.dirname(__file__)))
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, f"w{i}"], env=env, stdout=subprocess.PIPE, text=True)
             for i in range(13)]
    times = [float(p.communicate(timeout=60)[0].strip()) for p in procs]
    assert all(p.returncode == 0 for p in procs)
    assert max(times) < 1.0, times
    assert len(WindowStore(str(tmp_path)).hashes()) == 13
