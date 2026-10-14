"""The per-window settings files: one file per camera window, and no waiting between windows that close
at the same moment."""
import os
import subprocess
import sys

import pytest

from util import WindowStore, settings_dir


@pytest.fixture
def store(tmp_path):
    return WindowStore(str(tmp_path))


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


def test_settings_dir_is_qts_user_settings_folder():
    """Under the tests conftest points Qt's user settings at a temp dir; the store's folder follows it."""
    d = settings_dir()
    assert d and os.path.basename(d) == "nova" and not d.startswith(os.path.expanduser("~/.config"))
    assert WindowStore().dir == os.path.join(d, "windows")


WRITER = """
import sys, time
from util import WindowStore
t0 = time.monotonic()
WindowStore(sys.argv[2]).write(sys.argv[1], "1,2,3,4", "0,0,1,1")
print(f"{time.monotonic() - t0:.3f}")
"""


def test_thirteen_windows_closing_at_once_do_not_wait_for_each_other(tmp_path):
    """Each window has its own file: thirteen simultaneous writers all finish at once (the shared
    QSettings file made the last of them wait seconds, the lock being retried with growing sleeps)."""
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(__file__)))
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, f"w{i}", str(tmp_path)], env=env,
                              stdout=subprocess.PIPE, text=True) for i in range(13)]
    times = [float(p.communicate(timeout=60)[0].strip()) for p in procs]
    assert all(p.returncode == 0 for p in procs)
    assert max(times) < 1.0, times
    assert len(WindowStore(str(tmp_path)).hashes()) == 13
