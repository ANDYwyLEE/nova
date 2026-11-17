"""nova desktop install / remove: the desktop entry and the icon a Wayland desktop's task bar goes by."""
import os
import shutil
import subprocess

import pytest

import cams

LINUX = os.sys.platform.startswith("linux")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    return tmp_path / "share"


@pytest.mark.skipif(not LINUX, reason="desktop entries are Linux's")
def test_install_writes_the_entries_and_the_icon_and_remove_takes_them_away(home, capsys):
    """On a Raspberry Pi's desktop (labwc, a Wayland panel) Nova's windows had a blank icon on the task
    bar: the panel is told the application id `nova` and looks for nova.desktop and its Icon."""
    assert not cams.desktop_installed()
    assert cams.main(["desktop", "install"]) == 0
    said = capsys.readouterr().out
    assert "nova.desktop" in said and "nova desktop remove" in said
    entry = (home / "applications" / "nova.desktop").read_text()
    assert "Icon=nova\n" in entry and "StartupWMClass=nova\n" in entry and "NoDisplay" not in entry
    assert f'Exec="{os.path.join(cams.HERE, "nova")}" start\n' in entry
    native = (home / "applications" / "nova.wayland.desktop").read_text()        # the id of a native Wayland window
    assert "StartupWMClass=nova.wayland\n" in native and "NoDisplay=true\n" in native and "Icon=nova\n" in native
    for size in (16, 24, 32, 48, 64, 128, 256):
        copy = home / "icons" / "hicolor" / f"{size}x{size}" / "apps" / "nova.png"
        assert copy.read_bytes() == open(os.path.join(cams.HERE, "icons", f"nova-{size}.png"), "rb").read()
    assert (home / "icons" / "hicolor" / "scalable" / "apps" / "nova.svg").stat().st_size > 0
    assert cams.desktop_installed()
    assert cams.main(["desktop", "install"]) == 0                              # again: the same files
    capsys.readouterr()

    other = home / "applications" / "other.desktop"
    other.write_text("[Desktop Entry]\n")
    assert cams.main(["desktop", "remove"]) == 0
    assert f"removed {len(cams.desktop_files())} file(s)" in capsys.readouterr().out
    assert other.exists() and not cams.desktop_installed()
    assert not any(p.name.startswith("nova") for p in home.rglob("*") if p.is_file())
    assert cams.main(["desktop", "remove"]) == 0 and "nothing of Nova's" in capsys.readouterr().out


@pytest.mark.skipif(not LINUX, reason="desktop entries are Linux's")
def test_the_entries_are_valid_whatever_the_folder_is_called(home, tmp_path, monkeypatch):
    """Exec is quoted by the specification's two rules: a folder with a space, a quote, a dollar sign."""
    odd = '/home/a b/no"va$x`y\\z%1'
    monkeypatch.setattr(cams, "HERE", odd)
    line = [x for x in cams.desktop_entry("nova", False).splitlines() if x.startswith("Exec=")][0]
    assert line == 'Exec="/home/a b/no\\\\"va\\\\$x\\\\`y\\\\\\\\z%%1/nova" start'
    if not shutil.which("desktop-file-validate"):
        pytest.skip("desktop-file-validate is not installed")
    for app_id, hidden in cams.DESKTOP_IDS:
        path = tmp_path / f"{app_id}.desktop"
        path.write_text(cams.desktop_entry(app_id, hidden))
        done = subprocess.run(["desktop-file-validate", str(path)], capture_output=True, text=True)
        assert done.returncode == 0, done.stdout + done.stderr


def test_the_command_says_what_it_takes(home, monkeypatch):
    with pytest.raises(SystemExit) as e:
        cams.main(["desktop"])
    assert "nova desktop install | remove" in str(e.value)
    monkeypatch.setattr(cams.sys, "platform", "darwin")
    with pytest.raises(SystemExit) as e:
        cams.main(["desktop", "install"])
    assert "for Linux desktops" in str(e.value) and not home.exists()


def test_nova_doctor_names_the_command_on_a_wayland_desktop(home, tmp_path, monkeypatch):
    import doctor
    monkeypatch.setattr(doctor, "LINUX", True)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    said = lambda: [t for _, t in doctor.checks("skipped", "", "", None, None, str(tmp_path / "none.ini"))]
    assert [t for t in said() if "nova desktop install" in t]
    (home / "applications").mkdir(parents=True)
    (home / "applications" / "nova.desktop").write_text("[Desktop Entry]\n")
    assert not [t for t in said() if "nova desktop install" in t]
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    (home / "applications" / "nova.desktop").unlink()
    assert not [t for t in said() if "nova desktop install" in t]             # an X11 panel is handed the icon
