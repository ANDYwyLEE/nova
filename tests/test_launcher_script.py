"""The `nova` shell script: it starts cams.py with the venv's Python, and says what is needed when
that venv was made with a Python older than Nova's."""
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the shell launcher is for Linux and macOS")


def _tree(tmp_path, version):
    """A copy of the launcher next to a venv whose Python is a script that prints what it was given."""
    shutil.copy(os.path.join(ROOT, "nova"), tmp_path / "nova")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    if version:
        (tmp_path / "venv" / "lib" / f"python{version}" / "site-packages").mkdir(parents=True)
    py = tmp_path / "venv" / "bin" / "python"
    py.write_text('#!/bin/sh\necho "python ran: $*"\n')
    py.chmod(0o755)
    return subprocess.run(["sh", str(tmp_path / "nova"), "status"], capture_output=True, text=True)


@pytest.mark.parametrize("version", ["3.9", "3.11"])
def test_a_venv_of_an_older_python_is_named_and_not_started(tmp_path, version):
    r = _tree(tmp_path, version)
    assert r.returncode == 1
    assert "python ran" not in r.stdout
    assert f"Python {version}" in r.stderr and "3.12 or newer" in r.stderr and "docs/install.md" in r.stderr


@pytest.mark.parametrize("version", ["3.12", "3.15", None])
def test_a_venv_of_a_python_that_fits_runs_cams(tmp_path, version):
    r = _tree(tmp_path, version)               # None: a venv without a lib/python3.N folder is not judged
    assert r.returncode == 0 and r.stderr == ""
    assert r.stdout.strip() == f"python ran: {os.path.realpath(tmp_path)}/cams.py status"     # realpath: macOS' /private
